"""
This module exposes some Linux syscalls, which can be used to set up mounts,
namespaces, and other low-level things. This only works on Linux and requires
Python to be compiled against a libc that exports a syscall function (which
almost all of them do).
"""

import ctypes
import os
import platform
from typing import Optional

CLONE_NEWNS = 0x00020000     # New mount namespace group
CLONE_NEWCGROUP = 0x02000000 # New cgroup namespace
CLONE_NEWUTS = 0x04000000    # New utsname namespace
CLONE_NEWIPC = 0x08000000    # New ipc namespace
CLONE_NEWUSER = 0x10000000   # New user namespace
CLONE_NEWPID = 0x20000000    # New pid namespace
CLONE_NEWNET = 0x40000000    # New network namespace
CLONE_NEWTIME = 0x00000080   # New time namespace (intersecs with CSIGNAL)

MNT_FORCE = 0x00000001       # Attempt to forcibly umount
MNT_DETACH = 0x00000002      # Just detach from the tree
MNT_EXPIRE = 0x00000004      # Mark for expiry
UMOUNT_NOFOLLOW = 0x00000008 # Do not follow symlinks when resolving umount path

MS_RDONLY = 1                # Mount read-only
MS_NOSUID = 2                # Ignore suid and sgid bits
MS_NODEV = 4                 # Disallow access to device special files
MS_NOEXEC = 8                # Disallow program execution
MS_SYNCHRONOUS = 16          # Writes are synced at once
MS_REMOUNT = 32              # Alter flags of a mounted FS
MS_MANDLOCK = 64             # Allow mandatory locks on an FS
MS_DIRSYNC = 128             # Directory modifications are synchronous
MS_NOSYMFOLLOW = 256         # Do not follow symlinks
MS_NOATIME = 1024            # Do not update access times
MS_NODIRATIME = 2048         # Do not update directory access times
MS_BIND = 4096               # Create a bind mount
MS_MOVE = 8192               # Move mount elsewhere
MS_REC = 16384               # Mount recursively 
MS_POSIXACL = (1<<16)        # VFS does not apply the umask
MS_UNBINDABLE = (1<<17)      # change to unbindable
MS_PRIVATE = (1<<18)         # change to private
MS_SLAVE = (1<<19)           # change to slave
MS_SHARED = (1<<20)          # change to shared
MS_RELATIME = (1<<21)        # Update atime relative to mtime/ctime
MS_KERNMOUNT = (1<<22)       # this is a kern_mount call
MS_I_VERSION = (1<<23)       # Update inode I_version field
MS_STRICTATIME = (1<<24)     # Always perform atime updates
MS_LAZYTIME = (1<<25)        # Update the on-disk [acm]times lazily

match platform.machine():
    case "aarch64":
        __NR_UMOUNT2 = 39
        __NR_MOUNT = 40
        __NR_PIVOTROOT = 41
        __NR_UNSHARE = 97
        __NR_SETHOSTNAME = 161
        __NR_SETDOMAINNAME = 162
        ARCH = "arm64"
        OS = "linux"
        VARIANT = "v8"
    case "x86_64":
        __NR_UMOUNT2 = 166
        __NR_MOUNT = 165
        __NR_PIVOTROOT = 155
        __NR_UNSHARE = 272
        __NR_SETHOSTNAME = 170
        __NR_SETDOMAINNAME = 171
        ARCH = "amd64"
        OS = "linux"
        VARIANT = ""
    case _:
        raise NotImplementedError("Platform %s not supported" % platform.machine())

libc = ctypes.CDLL(None, use_errno=True)
_syscall = libc.syscall
_syscall.restype = ctypes.c_long
_syscall.argtypes = [ctypes.c_long]

def syscall(number: int, *args) -> int:
    """Python wrapper around the syscall function that raises an `OSError` when
    the call failed. Caller is responsible for ensuring that all arguments are
    types from the ctypes module."""

    ret = _syscall(ctypes.c_long(number), *args)

    if ret == -1:
        # If the syscall function returns -1, the call failed and errno indicates why.
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))

    return ret

def unshare(flags: int) -> int:
    """Disassociate parts of the process execution context. The flags argument
    is a bit mask that specifies which parts of the execution context should be
    unshared."""

    return syscall(__NR_UNSHARE,
                   ctypes.c_ulong(flags))

def mount(source: str, target: str, type: str, flags: int, data: Optional[str]):
    """Attach the filesystem specified by source to the location specified by
    the pathname in target."""

    return syscall(__NR_MOUNT,
                   ctypes.c_char_p(source.encode()),
                   ctypes.c_char_p(target.encode()),
                   ctypes.c_char_p(type.encode()),
                   ctypes.c_ulong(flags),
                   ctypes.c_char_p(data.encode() if data is not None else None))

def umount(target: str, flags: int = 0):
    """Remove the attachment of the topmost filesystem mounted on target. The
    behavior of the operation can be tuned using various flags."""

    return syscall(__NR_UMOUNT2,
                   ctypes.c_char_p(target.encode()),
                   ctypes.c_int(flags))

def pivot_root(new_root: str, put_old: str):
    """Move the root mount to the directory `put_old` and make `new_root` the
    new root mount."""

    return syscall(__NR_PIVOTROOT,
                   ctypes.c_char_p(new_root.encode()),
                   ctypes.c_char_p(put_old.encode()))

def sethostname(name: str):
    """Set the hostname to the value given to this function."""

    return syscall(__NR_SETHOSTNAME,
                   ctypes.c_char_p(name.encode()),
                   ctypes.c_int(len(name)))

def setdomainname(name: str):
    """Set the domain name to the value given to this function."""

    return syscall(__NR_SETDOMAINNAME,
                   ctypes.c_char_p(name.encode()),
                   ctypes.c_int(len(name)))