Podder
======

Podder is a basic layer-based container runtime. It can pull layers from
container registries that support the OCI standard (such as Dockerhub) and run
those layers on your device.

The primary goal of Podder was to run unprivileged containers of which all
layers are stored on disk, except for the last writable layer, which is fully
kept in memory. I also liked learning Linux-internals.

If you are looking to run containers, you are probably better off using `podman`
or `docker`. If you like learning Linux-internals, you may be interested in this
code. Use it at your own risk.

Currently Podder only supports arm64 and amd64 architectures.

# Quick start
Clone the repository and pull the Ubuntu container from Dockerhub:
```
python3 -m podder pull registry-1.docker.io/library/ubuntu:latest
```

Then, having pulled all the layers, run the container:
```
python3 -m podder start ubuntu
```

One can run the following to obtain a wheel from podder, which can be installed
to get the `podder` command on your path:
```
python3 -m build -n --wheel
```

# Layer structure
Layers are stored by default in `~/.local/share/podder` as directories (each
directory corresponds to one layer). You can just browse that directory and toy
around with the layers. A layer consists of:
- A `config.ini`, which configures how the layer should be started.
- A `merged` directory, which is used as root directory when the container is
  started.
- A `parent` symlink, which points to the layer which is the parent of this
  layer. The parent layer is put below this layer in the overlay file system.
- A `root` directory, which stores the actual contents of the layer. This
  directory is populated from a namespaced environment, so it cannot be shared
  across users.
- A `run` directory, which stores nothing. It is used to mount a tmpfs when the
  layer is started (and since that tmpfs sits in a mount namespace, it will be
  invisible to you).
- A `init.pid`, which contains the pid of the init process in the layer (if it
  is running).
All these compontents are optional.

Layers pulled from a registory are by default emphemeral, which means that all
modifications are kept in memory. This can be configured via `config.ini`.
`podder create` can be used to create a new layer on top of a previous one. Keep
in mind that the configuration of a parent is used as the configuration of a
layer, except for keys overwritten in `config.ini`.

# To be implemented
Besides the usual code quality improvements, the followings aspects are still to
be added:
- Executing a command in a running layer
- Network support for unprivileged containers via a binary with sufficient
  capabilities
- Make all commands self-explanitory, i.e., running `podder start --help` gives
  extensive documentation about how `podder start` should be used.
- Document the structure of `config.ini` and the integration in systemd.
- Parse `/etc/subuid` and `/etc/subgid` correctly, since one user can have
  multiple entries.

I do not want to use any packages besides the standard Python library and I am
not interested in developing Podder beyond just "executing containers" (i.e., it
will not start orchestrating them).