"""
Optional local entrypoint for dry-running the same strategy loop.

For Dhan Cloud deployment, use main_cloud.py.
"""

from signal_generator import run_signal_generator


if __name__ == "__main__":
    run_signal_generator()
