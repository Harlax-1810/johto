from .app import run

if __name__ == "__main__":
    import sys
    run(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765, open_browser="--no-open" not in sys.argv)
