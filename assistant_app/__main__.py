import argparse

import uvicorn

from .api import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    uvicorn.run(create_app(args.data_dir), host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
