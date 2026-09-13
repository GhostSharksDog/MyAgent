# ============================================================
# JobPilot 后端镜像
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
# 编译期依赖（gcc、头文件）在运行期完全不需要，但会产生几百 MB 体积。
# 多阶段把"装依赖"和"跑服务"分开，运行镜像里只有 site-packages 和代码。
# 这不只是省空间 —— **攻击面也跟着变小**（镜像里没有编译器）。
# ============================================================
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先只复制依赖清单再装依赖，让这一层能被 Docker 缓存。
# 如果和源码一起复制，那么改一行代码就会让"重装全部依赖"重新执行 ——
# 这是 Docker 构建里最常见的、代价最大的缓存失效。
COPY services/api/pyproject.toml ./pyproject.toml
COPY services/api/README.md* ./

# 只装第三方依赖，不装本项目（源码还没复制进来）
RUN pip install --prefix=/install \
    $(python - <<'PY'
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
deps = data.get("project", {}).get("dependencies", [])
print(" ".join(d for d in deps if not d.startswith("jobpilot")))
PY
)

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

# data/ 是挂载点：简历、岗位库、记忆落盘都在这里。
# 属于**运行时数据**，绝不能打进镜像 —— 一是会让镜像随数据膨胀，
# 二是简历属于个人信息，不该被分发到任何镜像仓库。
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# 默认启动 API 服务。rag 与 worker 在 compose 里覆盖 command。
#
# 注意这里**不做 HEALTHCHECK**：compose 里的健康检查比镜像内的更灵活
# （可以换地址、调间隔、多环境不同），而且 Python 镜像里没有 curl。
# 检查写在 compose 的 healthcheck 里。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
