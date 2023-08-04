import os
import pwd
import shlex
import signal
import sys
from typing import Dict, List, NoReturn, Optional
from config import load_config, write_config
import linux
import termios

LAYERPATH = "/home/ubuntu/layers"

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

    if retuid != 0:
        # newuidmap failed, set it up ourselves
        f = os.open(f"/proc/{pid}/uid_map", os.O_WRONLY)
        os.write(f, ("%8u %8u %8u\n" % (0, uid, 1)).encode())
        os.close(f)

    if retgid != 0:
        # newgidmap failed, set it up ourselves
        f = os.open(f"/proc/{pid}/setgroups", os.O_WRONLY)
        os.write(f, "deny".encode())
        os.close(f)

        f = os.open(f"/proc/{pid}/gid_map", os.O_WRONLY)
        os.write(f, ("%8u %8u %8u\n" % (0, gid, 1)).encode())
        os.close(f)

class Layer:
    path: str
    parent: Optional["Layer"]
    config: Dict

    @property
    def env(self) -> Dict[str, str]:
        value = self["environment"]
        if value is not None:
            return value
        else:
            return {}
    
    @property
    def cmd(self) -> List[str]:
        value = self["cmd"]
        if value is not None:
            return shlex.split(value)
        else:
            return []
    
    @property
    def hostname(self) -> str:
        value = self["hostname"]
        if value is not None:
            return value
        else:
            return os.path.basename(self.path)
    
    @property
    def domainname(self) -> str:
        value = self["domainname"]
        if value is not None:
            return value
        else:
            return "(none)"
    
    @property
    def ephemeral(self) -> bool:
        return bool(self["ephemeral"])
    
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

        for which in ("merged", "root", "tmpdir"):
            os.makedirs(os.path.join(self.path, which), exist_ok=True)
        if os.path.islink(os.path.join(self.path, "parent")):
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

        if os.path.exists(os.path.join(self.path, "parent")):
            parentpath = os.path.realpath(os.path.join(self.path, "parent"))
            self.parent = Layer(parentpath)

        if os.path.exists(os.path.join(self.path, "config.ini")):
            self.config = load_config(os.path.join(self.path, "config.ini"))
    
    def start(self) -> NoReturn:
        # Before doing anything, see whether location exists and there is no pidfile
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"{self.path}")
        if os.path.exists(self.pidfile):
            raise FileExistsError(f"{self.pidfile}")

        flags = linux.CLONE_NEWNS | linux.CLONE_NEWCGROUP | linux.CLONE_NEWUTS | \
            linux.CLONE_NEWIPC | linux.CLONE_NEWUSER | linux.CLONE_NEWPID | \
            linux.CLONE_NEWNET | linux.CLONE_NEWTIME

        r, w = os.pipe()
        if os.fork() == 0:
            os.close(w)

            # Wait for the parent to unshare
            _ = os.read(r, 1)

            setup_uidgidmap(os.getppid())
            
            os._exit(0)
        else:
            os.close(r)

        linux.unshare(flags)

        # Signal child that we have unshared by closing the pipe
        os.close(w)
        os.wait()

        # Forking is required to get a process with PID 1 in place. The parent
        # stays alive to record the pid in the parent namespace in a pid file.
        # Before forking, open the directory where the pid file must be written
        # (since the child will modify mounts) and record all terminal
        # attributes (since the child may modify those)
        dir_fd = os.open(self.path, os.O_DIRECTORY)
        attr = termios.tcgetattr(sys.stdin.fileno())
        pid = os.fork()
        if pid > 0:
            # Create a pid file
            f = os.open("init.pid", os.O_CREAT | os.O_WRONLY, dir_fd=dir_fd)
            os.write(f, f"{pid}\n".encode())
            os.close(f)

            while True:
                pid, status = os.waitpid(pid, 0)

                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    # Remove the pid file
                    os.remove("init.pid", dir_fd=dir_fd)

                    # Restore the terminal to its original state; this will
                    # send SIGTTOU since this is a background process, which
                    # we ignore.
                    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attr)

                    if os.WIFEXITED(status):
                        # Exit with the same status code as init did
                        sys.exit(os.waitstatus_to_exitcode(status))
                    if os.WIFSIGNALED(status):
                        # Exit with 128 + the signal number, a convention used by bash
                        sys.exit(128 + os.WTERMSIG(status))
        else:
            os.close(dir_fd)

        old_root = "old_root"

        # Build up the layers using an overlayfs. All the changeable folders of
        # the overlayfs are put in a tmpfs, which is destroyed whenever the
        # mount namespace ceases to exist.
        # FIXME: should be done based on "ephemeral" configuration
        overlay = self.overlay()
        linux.mount("none", os.path.join(self.path, "tmpdir"), "tmpfs", 0, None)
        os.mkdir(os.path.join(self.path, "tmpdir", "work"))
        os.mkdir(os.path.join(self.path, "tmpdir", "upper"))
        linux.mount("none",
                    os.path.join(self.path, "merged"),
                    "overlay",
                    0,
                    f"lowerdir={':'.join(overlay)},upperdir={os.path.join(self.path, 'tmpdir', 'upper')},workdir={os.path.join(self.path, 'tmpdir', 'work')},userxattr")
        
        os.mkdir(os.path.join(self.path, "merged", old_root))
        try:
            linux.pivot_root(os.path.join(self.path, "merged"), os.path.join(self.path, "merged", old_root))
            try:
                os.chdir("/")

                # Ensure mount events in old root remain in this namespace
                linux.mount("ignored", f"/{old_root}", "ignored", linux.MS_SLAVE | linux.MS_REC, None)

                # Mount /proc and /sys
                linux.mount("none", "/proc", "proc", 0, None)
                try:
                    # This only works when we have a network namespace
                    linux.mount("none", "/sys", "sysfs", 0, None)
                except PermissionError:
                    linux.mount(f"/{old_root}/sys", "/sys", "ignored", linux.MS_BIND | linux.MS_REC, None)

                # Populate a /dev directory
                linux.mount("none", "/dev", "tmpfs", 0, None)
                os.symlink("/proc/self/fd", "/dev/fd")
                os.symlink("/proc/self/fd/0", "/dev/stdin")
                os.symlink("/proc/self/fd/1", "/dev/stdout")
                os.symlink("/proc/self/fd/2", "/dev/stderr")
                os.mkdir("/dev/shm")
                linux.mount("none", "/dev/shm", "tmpfs", 0, None)

                for what in ("null", "zero", "full", "random", "urandom", "tty"):
                    # Ensure the file exists
                    open(f"/dev/{what}", mode='w').close()

                    # Do the bind mount
                    linux.mount(f"/{old_root}/dev/{what}", f"/dev/{what}", "ignored", linux.MS_BIND, None)

                # Create some temporary directories
                linux.mount("none", "/tmp", "tmpfs", 0, None)
                linux.mount("none", "/run", "tmpfs", 0, None)
            finally:
                # Unmount the old root
                linux.umount(f"/{old_root}", linux.MNT_DETACH)
        finally:
            # Remove the old root directory
            os.rmdir(f"/{old_root}")
        
        linux.sethostname(self.hostname)
        linux.setdomainname(self.domainname)
        
        os.execvpe(self.cmd[0], self.cmd, self.env)

if __name__ == "__main__":
    layer = Layer("ubuntu")
    layer.start()
