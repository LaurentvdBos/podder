from datetime import datetime
import os
import stat
import struct
from typing import BinaryIO, Dict, Generator

class TarFile:
    def __init__(self, path: bytes, mode: bytes, uid: bytes, gid: bytes, size: bytes,
                 mtime: bytes, checksum: bytes, type: bytes, linkpath: bytes, ustar: bytes,
                 ustarv: bytes, uname: bytes, gname: bytes, major: bytes, minor: bytes, prefix: bytes):
        decode = lambda b: b.decode().split('\0', 1)[0]

        if ustar != b'ustar\0' or ustarv != b'00':
            raise ValueError(f"ustar is {ustar}; ustarv is {ustarv}")

        self.path = os.path.join(decode(prefix), decode(path))
        self.mode = int(decode(mode), base=8)
        self.uid = int(decode(uid), base=8)
        self.gid = int(decode(gid), base=8)
        self.size = int(decode(size), base=8)
        self.mtime = float(int(decode(mtime), base=8))
        self.checksum = decode(checksum)
        self.type = chr(type[0])
        self.linkpath = decode(linkpath)
        self.uname = decode(uname)
        self.gname = decode(gname)
        self.major = decode(major)
        self.minor = decode(minor)

        # The following fields can only be added via pax headers
        self.ctime = None
        self.atime = None

        # Data should be populated manually later
        self.data = b''
    
    def __repr__(self) -> str:
        return "<TarFile %o %c %s %d %d %s (%d bytes)>" % (self.mode, self.type, self.path, self.uid, self.gid, str(datetime.fromtimestamp(self.mtime)), self.size)
    
    def write(self, path: str):
        if (os.path.exists(os.path.join(path, self.path)) or os.path.islink(os.path.join(path, self.path))) and not os.path.isdir(os.path.join(path, self.path)):
            raise FileExistsError(self.path)

        dir_fd = os.open(path, os.O_DIRECTORY)
        try:
            match self.type:
                case '0' | '7':
                    f = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, self.mode, dir_fd=dir_fd)
                    os.write(f, self.data)
                    os.utime(f, times=(self.atime if self.atime is not None else self.mtime, self.mtime))
                    os.chown(f, self.uid, self.gid)
                    os.close(f)
                
                case '1':
                    os.link(self.linkpath, self.path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                
                case '2':
                    os.symlink(self.linkpath, self.path, dir_fd=dir_fd)
                
                case '3':
                    os.mknod(self.path, self.mode | stat.S_IFCHR, os.makedev(self.major, self.minor), dir_fd=dir_fd)
                
                case '4':
                    os.mknod(self.path, self.mode | stat.S_IFBLK, os.makedev(self.major, self.minor), dir_fd=dir_fd)
                
                case '5':
                    if os.path.isdir(os.path.join(path, self.path)):
                        os.chmod(self.path, self.mode, dir_fd=dir_fd)
                    else:
                        os.mkdir(self.path, self.mode, dir_fd=dir_fd)
                
                case _:
                    raise NotImplementedError(f"File type {self.type} unknown")
        finally:
            os.close(dir_fd)


def unpax(pax: bytes) -> Dict[str, str]:
    ret = {}

    i = 0
    while i < len(pax):
        # Read the length (an integer)
        s = pax.find(b' ', i)
        if s == -1:
            raise ValueError(f"Could not parse pax: {pax}")
        
        # Pull the block from the pax header
        length = int(pax[i:s])
        block = pax[i:i+length]
        i += length

        # Split it up in length (ignored) and a key=val pair
        _, block = block.split(b' ', 1)
        key, val = block.split(b'=', 1)

        # Assign and remove the last new line
        ret[key.decode()] = val[:-1].decode()
    
    return ret

def untar(fp: BinaryIO) -> Generator[TarFile, None, None]:
    pax_x = {}
    pax_g = {}

    while len(block := fp.read(512)) > 0:
        if all((b == 0 for b in block)):
            # It is an all-zero block
            continue

        # Unpack the data in a tuple
        fmt = "100s8s8s8s12s12s8sc100s6s2s32s32s8s8s155s12x"
        t = struct.unpack(fmt, block)

        # Create the TarFile
        file = TarFile(*t)

        size = file.size
        if size % 512 > 0:
            size += 512 - size % 512
        data = fp.read(size)
        file.data = data[:file.size]

        # See whether we are dealing with a pax header
        if file.type == 'x' or file.type == 'g':
            pax = unpax(file.data)
            if file.type == 'x':
                pax_x = pax
            else:
                pax_g = pax

            # Don't yield pax headers
            continue
        else:
            # Assign any pax headers present
            pax = pax_g | pax_x
            PAX = {
                "ctime": float,
                "mtime": float,
                "atime": float,
                "uid": int,
                "gid": int
            }
            if 'size' in pax.keys():
                raise NotImplementedError("pax header has size key")
            for key, val in pax.items():
                if len(val) > 0:
                    fun = PAX.get(key, str)
                    setattr(file, key, fun(val))

            # Reset the x header
            pax_x = {}

        yield file

if __name__ == "__main__":
    with open("/home/ubuntu/foo.tar", "rb") as fp:
        for file in untar(fp):
            print(file)