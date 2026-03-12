#!/usr/bin/env python3
from samsung_control.main import main, install_for_user
import sys


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--install", "install"):
        install_for_user()
    else:
        main()
