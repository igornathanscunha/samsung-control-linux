import logging
import sys


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_paths = ["/var/log/samsung-control.log", "/tmp/samsung-control.log"]

    for log_path in log_paths:
        try:
            file_handler = logging.FileHandler(log_path)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            return
        except PermissionError:
            continue
        except Exception as e:
            print(f"Error setting up logging to {log_path}: {e}", file=sys.stderr)
            continue

    print("Warning: Could not set up file logging", file=sys.stderr)
