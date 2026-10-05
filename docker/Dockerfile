# ============================================================
# Legacy 后端镜像
#
# 【为什么三个服务共用一个 Dockerfile】
#
# 因为当前三个服务**共享同一份 Python 代码**（app.rag 被三类进程都用到）。
# 如果为每个服务各写一个 Dockerfile，它们的差异只有最后一行的启动命令 ——
# 三份几乎相同的构建脚本，改一处要记得改三处，迟早不一致。
#
# 真正独立之后（代码也搬开了），再拆成三个 Dockerfile 才是对的。
# 判断标准很简单：**构建步骤是否有实质差异**。
# 只差启动命令时，用 ARG/entrypoint 区分比复制三份文件更可靠。
#
# 【为什么用多阶段构建】
# 编译期依赖（gcc、头文件、node_modules）在运行期完全不需要，
# 但会产生几百 MB 体积。多阶段把"构建"和"跑服务"分开，
# 运行镜像里只有 site-packages、前端产物和代码。
# 这不只是省空间 —— **攻击面也跟着变小**（镜像里没有编译器，也没有 npm）。
# ============================================================

# ------------------------------------------------------------
# 阶段 1：前端构建（T22：前端产物必须进镜像）
#
# 【为什么这一步是"部署真能跑起来"的关键】
# 在这之前镜像里只有后端，而 main.py 找不到 apps/web/dist 时会**静默地**
# 只提供 API（那个宽容本来是为"开发时前端跑在 Vite 里"准备的）。
# 于是 `docker compose up` 之后：容器 healthy、接口 200、界面 404 ——
# 一个没有任何报错的"部署成功"。
# ------------------------------------------------------------
FROM node:22-alpine AS web

WORKDIR /web

# corepack 自带在 node 镜像里，用它装 pnpm 就不必再依赖 npm 全局安装
RUN corepack enable && corepack prepare pnpm@10 --activate

# 先只复制依赖清单，让这一层能被缓存（与下面 Python 侧同样的理由）
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY apps/web/ ./
# 产物落在 /web/dist（vite 的 outDir 相对 apps/web）
RUN pnpm build

# ------------------------------------------------------------
# 阶段 2：Python 依赖
# ------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先只复制依赖清单再装依赖，让这一层能被 Docker 缓存。
# 如果和源码一起复制，那么改一行代码就会让"重装全部依赖"重新执行 ——
# 这是 Docker 构建里最常见的、代价最大的缓存失效。
COPY services/api/pyproject.toml ./pyproject.toml
COPY services/api/README.md* ./

# 只装第三方依赖，不装本项目（源码还没复制进来）。
#
# 【这里踩过两个坑，都是"没构建过"才会留下的】
#  1. 过滤自身依赖时写死了旧项目名 `jobpilot`，改名后那句话永远为真；
#     现在从 pyproject 读项目名，改名不会再让它失效。
#  2. 原来是 `RUN pip install ... $(python - <<'PY' … PY\n)` ——
#     **Dockerfile 解析器直接报错**：`unknown instruction: )`。
#     heredoc 只能作为 RUN 的整段内容，不能这样嵌在 `$( )` 命令替换里。
#     改成"先生成 requirements.txt 再装"，两步都是最普通的 shell 写法；
#     依赖层的缓存效果不变（这两步只依赖 pyproject.toml）。
RUN python -c 'import pathlib,tomllib; p=tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))["project"]; n=p.get("name",""); pathlib.Path("/tmp/reqs.txt").write_text("\n".join(d for d in p.get("dependencies",[]) if not d.startswith(n)), encoding="utf-8")' \
 && pip install --prefix=/install -r /tmp/reqs.txt

# ------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8

# 非 root 运行。容器逃逸的代价和权限直接相关，
# 而这一点几乎不花成本 —— 没有理由不做。
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY services/api/app ./app
COPY services/api/pyproject.toml ./pyproject.toml
COPY scripts ./scripts

# 前端产物。**镜像里的目录结构与仓库不同**（代码在 /app/app/），
# 所以这里必须显式告诉应用去哪找 —— 靠 parents[3] 的推断在镜像里会落空，
# 而落空的后果是静默的"只有 API、没有界面"。
COPY --from=web /web/dist ./apps/web/dist
ENV WEB_DIST=/app/apps/web/dist

# data/ 是挂载点：简历、岗位库、记忆落盘都在这里。
# 属于**运行时数据**，绝不能打进镜像 —— 一是会让镜像随数据膨胀，
# 二是简历属于个人信息，不该被分发到任何镜像仓库。
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# 默认启动 API 服务。rag 与 worker 在 compose 里覆盖 command。
#
# ⚠ 这里的 `--host 0.0.0.0` 与 APP_HOST 是**必须成对**的：
# 访问控制检查判定"是否对外暴露"依据的是 APP_HOST，而真正决定监听地址的
# 是这一行。两者不一致时会出现最糟的一种情况 —— 容器监听所有网卡，
# 而启动日志说"仅监听回环地址，未启用密钥鉴权"（一个说反了的结论）。
# compose 里已设 APP_HOST=0.0.0.0，并有测试钉住这一对（test_service_split.py）。
#
# 注意这里**不做 HEALTHCHECK**：compose 里的健康检查比镜像内的更灵活
# （可以换地址、调间隔、多环境不同），而且 Python 镜像里没有 curl。
# 检查写在 compose 的 healthcheck 里。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
