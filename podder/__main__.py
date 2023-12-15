import argparse
import sys
from typing import NoReturn
import podder.layer as layer
import podder.registry as registry

def pull(args) -> int:
    url = args.url
    if not '/' in url:
        lay = layer.Layer(url)
        if lay.url is not None:
            url = lay.url
            print(f"Resolving {args.url} to {url}...")

    registry.pull(url)
    return 0

def start(args) -> NoReturn:
    lay = layer.Layer(args.layer)
    lay.start()

def create(args) -> int:
    lay = layer.Layer(args.layer, parent=layer.Layer(args.parent))
    lay.write()
    return 0

def exec(args) -> NoReturn:
    lay = layer.Layer(args.layer)
    lay.exec(args.cmd)

def network(args) -> int | NoReturn:
    lay = layer.Layer(args.layer)
    if lay.ifname is not None:
        import os
        import time
        import podder.linux as linux

        # FIXME: wait for the container to appear
        time.sleep(1)

        # Read the pidfile
        with open(lay.pidfile, 'r') as f:
            pid = int(f.read())

        if lay.mac is None:
            os.system(f"~/.local/bin/podder-net {lay.ifname} {str(pid)}")
        else:
            os.system(f"~/.local/bin/podder-net {lay.ifname} {str(pid)} {lay.mac}")
        
        if os.fork() == 0:
            # Join the network namespace (and also the user namespace to gain root and pid namespace to get killed automatically)
            flags = linux.CLONE_NEWNET | linux.CLONE_NEWUSER | linux.CLONE_NEWPID

            fd = os.pidfd_open(pid)
            linux.setns(fd, flags)
            os.close(fd)

            # Bring lo and macvlan0 up
            os.system("ip link set lo up")
            os.system("ip link set macvlan0 up")

            # Start DHCP
            os.execv("/usr/sbin/dhclient", ["-d", "-v", "macvlan0"])
        else:
            # Child stops automatically when pid namespace disappears
            os.wait()
            return 0
    else:
        print("No interface specified; nothing to do.")
        return 0

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--layerpath', help="""Path where the layers are stored.
                        This path is created if it does not exist (yet). The
                        default can be overridden using the $LAYERPATH
                        environment variable.""",
                        default=layer.LAYERPATH)

    subparsers = parser.add_subparsers(title="actions", required=True)

    parser_pull = subparsers.add_parser("pull", help="pull a set of layers from a registry")
    parser_pull.add_argument("url", help="reference to a registry to pull")
    parser_pull.set_defaults(func=pull)

    parser_start = subparsers.add_parser("start", help="start a layer")
    parser_start.add_argument("layer", help="layer to start")
    parser_start.set_defaults(func=start)

    parser_create = subparsers.add_parser("create", help="create a new layer")
    parser_create.add_argument("layer", help="layer name")
    parser_create.add_argument("--parent", help="parent layer, if any")
    parser_create.set_defaults(func=create)

    parser_exec = subparsers.add_parser("exec", help="execute in existing layer")
    parser_exec.add_argument("layer", help="layer name")
    parser_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="command to be executed")
    parser_exec.set_defaults(func=exec)

    parser_network = subparsers.add_parser("network", help="initialize network for existing layer")
    parser_network.add_argument("layer", help="layer name")
    parser_network.set_defaults(func=network)

    args = parser.parse_args()

    layer.LAYERPATH = args.layerpath

    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())