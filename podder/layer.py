from fcntl import ioctl
import os
import pwd
from select import select
import shlex
import signal
import sys
import tty
from typing import Dict, List, NoReturn, Optional
from podder.config import load_config, write_config
import podder.linux as linux
import termios

# The layerpath is $LAYERPATH, or $XDG_DATA_HOME/podder if that does not exist,
# or ~/.local/share/podder if that one does not exist.
LAYERPATH = os.getenv("LAYERPATH", os.path.join(os.getenv("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "podder"))

def setup_uidgidmap(pid: int):
    uid = os.geteuid()
    gid = os.getegid()
    user = pwd.getpwuid(uid)

    # Find the allowed ranged for the current user
    retuid = -1
    with open("/etc/subuid", "r") as f:
        for line in f:
            uidorname, subuid, subuidcount = line.split(":")
            if uidorname == user[0] or uidorname == str(uid):
                retuid = os.system(f"newuidmap {pid} 0 {uid} 1 1 {subuid} {subuidcount}")
                break
    
    retgid = -1
    with open("/etc/subgid", "r") as f:
        for line in f:
            uidorname, subgid, subgidcount = line.split(":")
            if uidorname == user[0] or uidorname == str(uid):
                retgid = os.system(f"newgidmap {pid} 0 {gid} 1 1 {subgid} {subgidcount}")
                break

    if retuid != 0:
        # newuidmap failed, set it up ourselves

        # root user can make arbitrary maps, so make the largest identity map
        # possible if uid == 0.
        # TODO: does this work in namespaces where not all uids are present? If
        # not, maybe we need to parse /proc/self/uid_map

        f = os.open(f"/proc/{pid}/uid_map", os.O_WRONLY)
        os.write(f, ("%8u %8u %8u\n" % (0, uid, 1 if uid != 0 else 4294967295)).encode())
        os.close(f)

    if retgid != 0:
        # newgidmap failed, set it up ourselves

        if uid != 0:
            # If we are not root, the setgroups syscall needs to be disabled.
            f = os.open(f"/proc/{pid}/setgroups", os.O_WRONLY)
            os.write(f, "deny".encode())
            os.close(f)

        f = os.open(f"/proc/{pid}/gid_map", os.O_WRONLY)
        os.write(f, ("%8u %8u %8u\n" % (0, gid, 1 if uid != 0 else 4294967295)).encode())
        os.close(f)

def forktochild(pidfile: Optional[str] = None, *, dir_fd: Optional[int] = None):
    """Fork and only return from the child. The parent waits for the child to
    exit and never returns, not even when an exception happens. If we are
    attached to a TTY, a pseudo-TTY is made for the child, so /dev/pts should be
    mounted in that case. If SIGTERM is sent to the parent, this signal is
    forwarded to the child."""

    if os.isatty(sys.stdin.fileno()):
        attr = termios.tcgetattr(sys.stdin.fileno())
        pid, fd = os.forkpty()
    else:
        pid = os.fork()
        fd = -1
    if pid > 0:
        exit_code = 1

        if pidfile is not None:
            # Create a pid file
            f = os.open(pidfile, os.O_CREAT | os.O_WRONLY, dir_fd=dir_fd)
            os.write(f, f"{pid}\n".encode())
            os.close(f)

        # Catch SIGTERM and send it to the child
        def sigterm(signum, frame):
            os.kill(pid, signum)

        signal.signal(signal.SIGTERM, sigterm)

        try:
            if fd > -1:
                tty.setraw(sys.stdin.fileno())

                def sigwinch(signum, frame):
                    winsz = ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b" " * 1024)
                    ioctl(fd, termios.TIOCSWINSZ, winsz)

                signal.signal(signal.SIGWINCH, sigwinch)
                signal.raise_signal(signal.SIGWINCH)

                stdin = b""
                stdout = b""
                while True:
                    rlist = []
                    wlist = []
                    if stdin:
                        wlist += [fd]
                    else:
                        rlist += [sys.stdin.fileno()]
                    if stdout:
                        wlist += [sys.stdout.fileno()]
                    else:
                        rlist += [fd]

                    rlist, wlist, _ = select(rlist, wlist, [])

                    if sys.stdin.fileno() in rlist:
                        stdin = os.read(sys.stdin.fileno(), 1024)
                        if not stdin:
                            os.close(fd)
                    
                    if fd in rlist:
                        try:
                            stdout = os.read(fd, 1024)
                        except OSError:
                            stdout = b""
                        if not stdout:
                            break
                    
                    if sys.stdout.fileno() in wlist:
                        n = os.write(sys.stdout.fileno(), stdout)
                        stdout = stdout[n:]
                    
                    if fd in wlist:
                        n = os.write(fd, stdin)
                        stdin = stdin[n:]

            _, status = os.waitpid(pid, 0)

            if os.WIFEXITED(status):
                # Exit with the same status code as init did
                exit_code = os.WEXITSTATUS(status)
            if os.WIFSIGNALED(status):
                # Exit with 128 + the signal number, a convention used by bash
                exit_code = 128 + os.WTERMSIG(status)
        finally:
            # Restore the terminal to its original state; this will
            # send SIGTTOU since this is a background process, which
            # we ignore.
            if fd > -1:
                signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attr)

            if pidfile is not None:
                # Remove the pid file
                os.remove(pidfile, dir_fd=dir_fd)

            # Exit the Python process
            sys.stdout.flush()
            os._exit(exit_code)

class Layer:
    path: str
    parent: Optional["Layer"]
    config: Dict

    @property
    def env(self) -> Dict[str, str]:
        value = self["env"]
        if value is not None:
            return value
        else:
            return {}
    
    @env.setter
    def env(self, value: Dict[str, str]):
        self.config["env"] = value
    
    @property
    def cmd(self) -> List[str]:
        value = self["cmd"]
        if value is not None:
            return shlex.split(value)
        else:
            return []
    
    @cmd.setter
    def cmd(self, value: str | List[str]):
        if isinstance(value, list):
            value = shlex.join(value)
        
        self.config["cmd"] = value
    
    @property
    def hostname(self) -> str:
        value = self["hostname"]
        if value is not None:
            return value
        else:
            return os.path.basename(self.path)
    
    @hostname.setter
    def hostname(self, value: str):
        self.config["hostname"] = value
    
    @property
    def domainname(self) -> str:
        value = self["domainname"]
        if value is not None:
            return value
        else:
            return "(none)"
    
    @domainname.setter
    def domainname(self, value: str):
        self.config["domainname"] = value
    
    @property
    def ephemeral(self) -> bool:
        return bool(self["ephemeral"])
    
    @ephemeral.setter
    def ephemeral(self, value: bool):
        if value:
            self.config["ephemeral"] = "yes"
        else:
            self.config["ephemeral"] = ""
    
    @property
    def url(self) -> str:
        return self["url"]
    
    @url.setter
    def url(self, value: str):
        self.config["url"] = str(value)

    @property
    def mac(self) -> str:
        return self["mac"]

    @mac.setter
    def mac(self, value: str):
        self.config["mac"] = str(value)

    @property
    def ifname(self) -> str:
        return self["ifname"]

    @ifname.setter
    def ifname(self, value: str):
        self.config["ifname"] = str(value)
    
    @property
    def pidfile(self) -> str:
        return os.path.join(self.path, "init.pid")

    def overlay(self) -> List[str]:
        """Get all layers needed to build a namespace with this layer as top layer."""

        if self.parent is not None:
            return [os.path.join(self.path, "root")] + self.parent.overlay()
        else:
            return [os.path.join(self.path, "root")]
    
    def __getitem__(self, key: str) -> Dict | str | None:
        """Return an effective configuration value of this layer. Effective
        means that it is merged with any configuration set by the parent. This
        function does not attempt to read any files (that all happens in
        __init__)."""

        if key in self.config.keys():
            value = self.config[key]

            if isinstance(value, str) or self.parent is None:
                # No need to merge with the parent
                return value
            
            # value is a dictionary, which potentially should be merged
            parentvalue = self.parent[key]

            if isinstance(parentvalue, dict):
                return parentvalue | value
            else:
                return value
        elif self.parent is not None:
            return self.parent[key]
        else:
            return None
    
    def write(self):
        """Write this layer to disk. It will overwrite any configuration or
        directories that are already present, but will not change anything in
        the root file system."""

        for which in ("merged", "root", "run"):
            os.makedirs(os.path.join(self.path, which), exist_ok=True)
        if os.path.lexists(os.path.join(self.path, "parent")):
            os.remove(os.path.join(self.path, "parent"))
        if self.parent is not None:
            os.symlink(self.parent.path, os.path.join(self.path, "parent"))
        if os.path.exists(os.path.join(self.path, "config.ini")):
            os.remove(os.path.join(self.path, "config.ini"))
        if len(self.config.keys()) > 0:
            write_config(os.path.join(self.path, "config.ini"), self.config)

    def __init__(self, path: str, *, parent: Optional["Layer"] = None):
        self.path = os.path.join(LAYERPATH, path)
        self.parent = parent
        self.config = {}

        if os.path.exists(os.path.join(self.path, "parent")) and parent is None:
            parentpath = os.path.realpath(os.path.join(self.path, "parent"))
            self.parent = Layer(parentpath)

        if os.path.exists(os.path.join(self.path, "config.ini")):
            self.config = load_config(os.path.join(self.path, "config.ini"))
    
    def start(self) -> NoReturn:
        # Before doing anything, see whether location exists and there is no pidfile
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        if os.path.exists(self.pidfile):
            # Check whether the layer is running, and if it does, do not continue
            with open(self.pidfile, mode='r') as f:
                pid = int(f.read())
                try:
                    os.kill(pid, 0)
                except OSError:
                    print(f"Could not find process with pid {pid}; did the layer crash?", file=sys.stderr)
                    os.remove(self.pidfile)
                else:
                    raise FileExistsError(self.pidfile)

        flags = linux.CLONE_NEWNS | linux.CLONE_NEWCGROUP | linux.CLONE_NEWIPC | linux.CLONE_NEWUSER | linux.CLONE_NEWPID
        if self.ifname is not None:
            flags |= linux.CLONE_NEWNET | linux.CLONE_NEWUTS

        fd = os.eventfd(0)
        if (pid := os.fork()) == 0:
            # Wait for the parent to unshare
            os.eventfd_read(fd)
            setup_uidgidmap(os.getppid())
            os._exit(0)

        linux.unshare(flags)

        # Signal child that we have unshared
        os.eventfd_write(fd, 1)
        _, status = os.waitpid(pid, 0)
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            raise RuntimeError("Child crashed")
        os.close(fd)

        # Ensure mount events in root remain in this namespace. By default,
        # Linux already marks this mount namespace as less privileged, since it
        # is owned by a user namespace other than the default one.
        linux.mount("ignored", "/", "ignored", linux.MS_PRIVATE | linux.MS_REC, None)

        # Take note of the directory where we need to write the pidfile to. This
        # file pointer will survive pivot_root.
        dir_fd = os.open(os.path.dirname(self.pidfile), os.O_DIRECTORY)
        
        # Build up the layers of the overlayfs. If the layer is ephemeral, the
        # top layer is put on a tmpfs.
        overlay = self.overlay()
        work = os.path.join(self.path, "run")
        userxattr = ",userxattr"
        if self.ephemeral:
            linux.mount("none", os.path.join(self.path, "run"), "tmpfs", 0, "mode=777")
            os.mkdir(os.path.join(self.path, "run", "work"))
            os.mkdir(os.path.join(self.path, "run", "upper"))
            work = os.path.join(self.path, "run", "work")
            overlay = [os.path.join(self.path, "run", "upper")] + overlay

            # tmpfs does not support user extended attributes, but since we
            # mounted the tmpfs ourselves, we can use regular ones (which are
            # supported)
            userxattr = ""
        if len(overlay) > 1:
            linux.mount("none",
                        os.path.join(self.path, "merged"),
                        "overlay",
                        0,
                        f"lowerdir={':'.join(overlay[1:])},upperdir={overlay[0]},workdir={work},xino=off{userxattr}")
        else:
            # If it is only one layer, there is nothing to overlay
            linux.mount(overlay[0], os.path.join(self.path, "merged"), "ignored", linux.MS_BIND, None)

        # Add bind mounts to configure network; we do this before the pivot root
        # such that symlinks resolve correctly.
        if self.ifname is not None:
            for what in ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf"):
                # Ensure the file exists
                open(os.path.join(self.path, "merged", what[1:]), mode='w').close()

                # Do the bind mount, resolving any symlinks the files may have
                linux.mount(os.path.realpath(what), os.path.join(self.path, "merged", what[1:]), "ignored", linux.MS_BIND, None)

        os.mkdir(os.path.join(self.path, "merged", "old_root"))
        try:
            linux.pivot_root(os.path.join(self.path, "merged"), os.path.join(self.path, "merged", "old_root"))
            os.chdir("/")
            try:
                # Populate a /dev directory. The mode=755 makes sure there is no
                # 'sticky' bit, which blocks writing to a device in dev with -EACCES
                linux.mount("none", "/dev", "tmpfs", linux.MS_NOSUID, "mode=755")
                os.symlink("/proc/self/fd", "/dev/fd")
                os.symlink("/proc/self/fd/0", "/dev/stdin")
                os.symlink("/proc/self/fd/1", "/dev/stdout")
                os.symlink("/proc/self/fd/2", "/dev/stderr")
                os.mkdir("/dev/shm")
                linux.mount("none", "/dev/shm", "tmpfs", linux.MS_NOSUID | linux.MS_NODEV, "mode=1777")

                for what in ("null", "zero", "full", "random", "urandom", "tty"):
                    # Ensure the file exists
                    open(f"/dev/{what}", mode='w').close()

                    # Do the bind mount
                    linux.mount(f"/old_root/dev/{what}", f"/dev/{what}", "ignored", linux.MS_BIND, None)
                
                os.mkdir("/dev/mqueue")
                linux.mount("none", "/dev/mqueue", "mqueue", linux.MS_NOSUID | linux.MS_NODEV | linux.MS_NOEXEC, None)

                # Initialize the pseudotty dev
                os.mkdir("/dev/pts")
                linux.mount("none", "/dev/pts", "devpts", 0, "newinstance,mode=620,ptmxmode=666,gid=5")
                os.symlink("pts/ptmx", "/dev/ptmx")

                forktochild(os.path.basename(self.pidfile), dir_fd=dir_fd)

                if os.isatty(sys.stdin.fileno()):
                    # Create /dev/console pointing to the pseudo tty
                    open("/dev/console", mode='w').close()
                    linux.mount(os.ttyname(sys.stdin.fileno()), "/dev/console", "ignored", linux.MS_BIND, None)

                # We are now pid 1, so mount /proc and /sys
                linux.mount("none", "/proc", "proc",  linux.MS_NODEV | linux.MS_NOSUID | linux.MS_NOEXEC, None)
                try:
                    # This only works when we have a network namespace
                    linux.mount("none", "/sys", "sysfs", 0, None)
                    linux.mount("none", "/sys/fs/cgroup", "cgroup2", 0, None)
                except PermissionError:
                    linux.mount("/old_root/sys", "/sys", "ignored", linux.MS_BIND | linux.MS_REC, None)
            finally:
                # Unmount the old root
                linux.umount("/old_root", linux.MNT_DETACH)
        finally:
            # Remove the old root directory
            os.rmdir(f"/old_root")
        
        # Close the file pointer to the layer
        os.close(dir_fd)

        os.execvpe(self.cmd[0], self.cmd, self.env)
    
    def exec(self, cmd: List[str]) -> NoReturn:
        # Before doing anything, see whether location exists and that there is a pidfile
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        if not os.path.exists(self.pidfile):
            raise FileNotFoundError(self.pidfile)
        
        with open(self.pidfile, 'r') as f:
            pid = int(f.read())
        
        flags = linux.CLONE_NEWNS | linux.CLONE_NEWCGROUP | linux.CLONE_NEWIPC | linux.CLONE_NEWUSER | linux.CLONE_NEWPID | linux.CLONE_NEWNET | linux.CLONE_NEWUTS

        fd = os.pidfd_open(pid)
        linux.setns(fd, flags)
        os.close(fd)

        forktochild()

        os.execvpe(cmd[0], cmd, self.env)