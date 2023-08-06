import argparse
import sys
import podder.layer as layer
import podder.registry as registry

def pull(args):
    url = args.url
    if not '/' in url:
        lay = layer.Layer(url)
        if lay.url is not None:
            url = lay.url
            print(f"Resolving {args.url} to {url}...")

    registry.pull(url)
    return 0

def start(args):
    lay = layer.Layer(args.layer)
    lay.start()

def create(args):
    lay = layer.Layer(args.layer, parent=layer.Layer(args.parent))
    lay.write()
    return 0

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--layerpath', help="path where the individual layers are stored", default=layer.LAYERPATH)

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

    args = parser.parse_args()

    layer.LAYERPATH = args.layerpath

    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())