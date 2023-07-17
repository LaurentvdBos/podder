import os
import shlex
import signal
import sys
from typing import Dict, List, NoReturn, Optional
from config import load_config, write_config
import linux
import termios

LAYERPATH = "/home/ubuntu/layers"

class Layer:
    location: str
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
            return os.path.basename(self.location)
    
    @property
    def domainname(self) -> str:
        value = self["domainname"]
        if value is not None:
            return value
        else:
            return "(none)"
    
    @property
    def pidfile(self) -> str:
        return os.path.join(self.location, "init.pid")

    def overlay(self) -> List[str]:
        if self.parent is not None:
            return [os.path.join(self.location, "root")] + self.parent.overlay()
        else:
            return [os.path.join(self.location, "root")]
    
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
        for which in ("merged", "root", "work"):
            os.makedirs(os.path.join(self.location, which), exist_ok=True)
        if os.path.exists(os.path.join(self.location, "parent")):
            os.remove(os.path.join(self.location, "parent"))
        if self.parent is not None:
            os.symlink(self.parent.location, os.path.join(self.location, "parent"))
        if os.path.exists(os.path.join(self.location, "config.ini")):
            os.remove(os.path.join(self.location, "config.ini"))
        if len(self.config.keys()) > 0:
            write_config(os.path.join(self.location, "config.ini"), self.config)

    def __init__(self, location: str):
        self.location = os.path.join(LAYERPATH, location)
        self.parent = None
        self.config = {}

        if os.path.exists(os.path.join(self.location, "parent")):
            parentpath = os.path.realpath(os.path.join(self.location, "parent"))
            self.parent = Layer(parentpath)

        if os.path.exists(os.path.join(self.location, "config.ini")):
            self.config = load_config(os.path.join(self.location, "config.ini"))
    
    def start(self) -> NoReturn:
        # Before doing anything, see whether location exists and there is no pidfile
        if not os.path.exists(self.location):
            raise FileNotFoundError(f"{self.location}")
        if os.path.exists(self.pidfile):
            raise FileExistsError(f"{self.pidfile}")

        flags = linux.CLONE_NEWNS | linux.CLONE_NEWCGROUP | linux.CLONE_NEWUTS | \
            linux.CLONE_NEWIPC | linux.CLONE_NEWUSER | linux.CLONE_NEWPID | \
            linux.CLONE_NEWNET | linux.CLONE_NEWTIME

        uid = os.geteuid()
        gid = os.getegid()

        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        pid = os.getpid()
        if os.fork() == 0:
            # Wait for the parent to unshare
            _ = os.read(r1, 1)

            # Set the uid/gid maps for the parent
            ret1 = os.system(f"newuidmap {pid} 0 {uid} 1 1 100000 65536")
            ret2 = os.system(f"newgidmap {pid} 0 {gid} 1 1 100000 65536")
            
            # Signal the parent whether the calls succeeded
            ret = (0 if ret1 == 0 else 1) | (0 if ret2 == 0 else 2)
            os.write(w2, ret.to_bytes(length=1, byteorder='big'))

            sys.exit(0)
        else:
            # Close unneeded pipes
            os.close(w2)
            os.close(r1)

        linux.unshare(flags)

        # Signal the child that we have unshared
        os.write(w1, b"\0")

        # Wait for the child to signal back that we can continue
        ret = os.read(r2, 1)

        os.close(r2)
        os.close(w1)

        if ret[0] & 1:
            # newuidmap failed, set it up ourselves
            f = os.open("/proc/self/uid_map", os.O_WRONLY)
            os.write(f, ("%8u %8u %8u\n" % (0, uid, 1)).encode())
            os.close(f)

        if ret[0] & 2:
            # newgidmap failed, set it up ourselves
            f = os.open("/proc/self/setgroups", os.O_WRONLY)
            os.write(f, "deny".encode())
            os.close(f)

            f = os.open("/proc/self/gid_map", os.O_WRONLY)
            os.write(f, ("%8u %8u %8u\n" % (0, gid, 1)).encode())
            os.close(f)

        # Forking is required to get a process with PID 1 in place. The parent
        # stays alive to record the pid in the parent namespace in a pid file.
        # Before forking, open the directory where the pid file must be written
        # (since the child will modify mounts) and record all terminal
        # attributes (since the child may modify those)
        dir_fd = os.open(self.location, os.O_DIRECTORY)
        attr = termios.tcgetattr(sys.stdin.fileno())
        pid = os.fork()
        if pid > 0:
            # Create a pid file
            f = os.open("init.pid", os.O_CREAT | os.O_WRONLY, dir_fd = dir_fd)
            os.write(f, f"{pid}\n".encode())
            os.close(f)

            while True:
                pid, status = os.waitpid(pid, 0)

                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    # Remove the pid file
                    os.remove("init.pid", dir_fd = dir_fd)

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

        overlay = self.overlay()
        if len(overlay) > 1:
            linux.mount("none",
                        os.path.join(self.location, "merged"),
                        "overlay",
                        0,
                        f"lowerdir={':'.join(overlay[1:])},upperdir={overlay[0]},workdir={os.path.join(self.location, 'work')},userxattr")
        else:
            linux.mount(overlay[0], os.path.join(self.location, "merged"), "ignored", linux.MS_BIND, None)
        
        os.mkdir(os.path.join(self.location, "merged", old_root))
        try:
            linux.pivot_root(os.path.join(self.location, "merged"), os.path.join(self.location, "merged", old_root))
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
    layer = Layer("slbash2")
    layer.start()