import sys
import socket
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('host', type=str, help="Where to host the server")
    parser.add_argument('port', type=int, help="Port to host the server")
    args = parser.parse_args()

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((args.host, args.port))
    except:
        print("Could not make a connection to the server [= magma.vim]")
        sys.exit(1)

    # TODO: start bidirectional socket server in `HOST:PORT`


if __name__ == '__main__':
    sys.exit(main())
