"""PyInstaller entry point; multiprocessing dispatch must precede application imports."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from app.desktop.entry import main

    raise SystemExit(main())
