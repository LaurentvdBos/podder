import gzip
import json
import os
import re
from typing import BinaryIO, Mapping, Optional
from layer import Layer, setup_uidgidmap
import urllib.request
import urllib.parse

import linux
from untar import untar

class PullError(Exception):
    pass

class BearerHandler(urllib.request.BaseHandler):
    token: str
    authorizing: bool

    def __init__(self, *, token: Optional[str] = None) -> None:
        super().__init__()

        self.authorizing = False
        self.token = token
    
    def http_request(self, req: urllib.request.Request):
        if self.token is not None and req.get_header("authorization") is not None:
            req.add_header("authorization", f"Bearer {self.token}")
        
        return req
    
    def http_error_401(self, req: urllib.request.Request, fp: BinaryIO, code: int, msg: str, hdrs: Mapping[str, str]):
        if self.authorizing:
            # Do not authorize if this handler is already busy (e.g., when called recursively)
            return None

        try:
            self.authorizing = True

            # Parse the authentication header
            pat = lambda name: f"{name}=\"(?P<{name}>[^\"]+)\",?"
            m: Optional[re.Match] = re.fullmatch(f"Bearer ({pat('realm')}|{pat('service')}|{pat('scope')})+", hdrs["www-authenticate"])
            if m:
                auth = m.groupdict()
            else:
                return None
            
            # Get the token
            realm = auth.pop('realm')
            qs = urllib.parse.urlencode(auth)
            authreq = urllib.request.Request(f"{realm}?{qs}")
            with self.parent.open(authreq) as authresp:
                self.token = json.load(authresp)['token']
            
            # Redo the request with the authorization header
            req.add_header("authorization", f"Bearer {self.token}")
            return self.parent.open(req)
        finally:
            self.authorizing = False

def pull(url: str):
    (url, rest) = url.split("/", 1)
    (name, reference) = rest.split(":", 1)

    opener = urllib.request.build_opener(BearerHandler)

    print("Retrieving available manifests...")
    req = urllib.request.Request(f"https://{url}/v2/{name}/manifests/{reference}",
                                 headers={"accept": "application/vnd.docker.distribution.manifest.list.v2+json"})
    with opener.open(req) as resp:
        manifest_list = json.load(resp)

    # Find the manifest belonging to this architecture
    for manifest in manifest_list['manifests']:
        if manifest['platform']['architecture'] == linux.ARCH and manifest['platform'].get('variant', linux.VARIANT) == linux.VARIANT and manifest['platform']['os'] == linux.OS:
            print("Retrieving manifest...")
            req = urllib.request.Request(f"https://{url}/v2/{name}/manifests/{manifest['digest']}",
                                         headers={"accept": manifest['mediaType']})
            with opener.open(req) as resp:
                manifest = json.load(resp)
            break
    else:
        archlist = [manifest['platform']['architecture'] + manifest['platform'].get('variant', '') for manifest in manifest_list['manifests']]
        raise PullError(f"Architecture {linux.ARCH} not present; found {', '.join(archlist)}")
    
    # Pull the configuration. There is one major difference between how OCI
    # treats images and how we do it: in OCI, there is only a single
    # configuration for all layers as a whole. Hence, we pull the configuration
    # and put that on the *last* layer.
    print("Retrieving configuration...")
    req = urllib.request.Request(f"https://{url}/v2/{name}/blobs/{manifest['config']['digest']}",
                                 headers={"accept": manifest['config']['mediaType']})
    with opener.open(req) as resp:
        configuration = json.load(resp)

    # Execute the pull in a user namespace
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    if (pid := os.fork()) == 0:
        linux.unshare(linux.CLONE_NEWUSER)

        # Signal parent we unshared by closing the pipe
        os.close(r1)
        os.close(w1)

        # Wait for parent to setup uid/gid map
        os.close(w2)
        os.read(r2, 1)
        os.close(r2)

        for layer in manifest["layers"]:
            print(f"Pulling {layer['digest']}...", flush=True)
            l = Layer(layer['digest'].split(':', 1)[-1])
            l.write()
            req = urllib.request.Request(f"https://{url}/v2/{name}/blobs/{layer['digest']}",
                                         headers={"accept": layer['mediaType']})
            with opener.open(req) as resp:
                if "gzip" in layer["mediaType"]:
                    resp = gzip.GzipFile(fileobj=resp, mode='rb')
                for file in untar(resp):
                    print(file)
                    basename = os.path.basename(file.path)
                    if basename.startswith(".wh."):
                        if basename == ".wh..wh..opq":
                            raise NotImplementedError("Opaque whiteouts are not implemented")
                        else:
                            basename = basename[4:]
                            print(f"Removing {os.path.join(os.path.dirname(file.path), basename)}...")
                            os.mknod(os.path.join(l.path, 'root', os.path.dirname(file.path), basename), os.makedev(0, 0))
                    else:
                        file.write(os.path.join(l.path, 'root'), overwrite=True)
        
        os._exit(0)
    
    # Wait for child to unshare
    os.close(w1)
    os.read(r1, 1)
    os.close(r1)

    setup_uidgidmap(pid)

    # Signal child it can continue by closing the pipe
    os.close(w2)
    os.close(r2)

    os.wait()

    return configuration

if __name__ == "__main__":
    foo = pull("registry-1.docker.io/pihole/pihole:latest")
    #foo = pull("ghcr.io/home-assistant/home-assistant:stable")