import gzip
import json
import os
import re
from typing import BinaryIO, Mapping, Optional
from podder.layer import Layer, setup_uidgidmap
import urllib.request
import urllib.parse

import podder.linux as linux
from podder.untar import untar

CONTTYPE_MANIFESTLIST = {"application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.index.v1+json"}
CONTTYPE_MANIFEST = {"application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"}
CONTTYPE_CONFIG = {"application/vnd.docker.container.image.v1+json", "application/vnd.oci.image.config.v1+json"}
CONTTYPE_IMAGE = {"application/vnd.docker.image.rootfs.diff.tar", "application/vnd.oci.image.layer.v1.tar"}
CONTTYPE_IMAGE_GZIP = {"application/vnd.docker.image.rootfs.diff.tar.gzip", "application/vnd.oci.image.layer.v1.tar+gzip"}

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

def pull(full_url: str):
    (url, rest) = full_url.split("/", 1)
    (name, reference) = rest.split(":", 1)

    opener = urllib.request.build_opener(BearerHandler)

    print("Retrieving available manifests...")
    req = urllib.request.Request(f"https://{url}/v2/{name}/manifests/{reference}",
                                 headers={"accept": ", ".join(CONTTYPE_MANIFESTLIST)})
    with opener.open(req) as resp:
        manifest_list = json.load(resp)
    
    if manifest_list['mediaType'] not in CONTTYPE_MANIFESTLIST:
        raise NotImplementedError(f"MediaType {manifest_list['mediaType']}")
    
    # Find the manifest belonging to this architecture
    for manifest in manifest_list['manifests']:
        if manifest['platform']['architecture'] == linux.ARCH and manifest['platform'].get('variant', linux.VARIANT) == linux.VARIANT and manifest['platform']['os'] == linux.OS:
            print("Retrieving manifest...")
            req = urllib.request.Request(f"https://{url}/v2/{name}/manifests/{manifest['digest']}",
                                         headers={"accept": ", ".join(CONTTYPE_MANIFEST)})
            with opener.open(req) as resp:
                manifest = json.load(resp)
            break
    else:
        archlist = [manifest['platform']['architecture'] + manifest['platform'].get('variant', '') for manifest in manifest_list['manifests']]
        raise FileNotFoundError(f"Architecture not supported; found {', '.join(archlist)}")

    # Verify that the media types present are the ones we have implemented
    if manifest['mediaType'] not in CONTTYPE_MANIFEST:
        raise NotImplementedError(f"MediaType {manifest['mediaType']}")
    if manifest['config']['mediaType'] not in CONTTYPE_CONFIG:
        raise NotImplementedError(f"MediaType {manifest['config']['mediaType']}")
    for layer in manifest['layers']:
        if layer['mediaType'] not in (CONTTYPE_IMAGE | CONTTYPE_IMAGE_GZIP):
            raise NotImplementedError(f"MediaType {layer['mediaType']}")
    
    # Pull the configuration. There is one major difference between how OCI v2
    # treats images and how we do it: in OCI, there is only a single
    # configuration for all layers as a whole. Hence, we pull the configuration
    # and put that on the *last* layer.
    # FIXME: seems we can always request an OCI config. Is that true?
    print("Retrieving configuration...")
    req = urllib.request.Request(f"https://{url}/v2/{name}/blobs/{manifest['config']['digest']}",
                                 headers={"accept": ", ".join(CONTTYPE_CONFIG)})
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

        lay = None
        for layer in manifest["layers"]:
            lay = Layer(layer['digest'].split(':', 1)[-1], parent=lay)
            if os.path.exists(lay.path):
                print(f"Skipping {layer['digest']}...")
                continue

            print(f"Pulling {layer['digest']}...")
            lay.write()
            req = urllib.request.Request(f"https://{url}/v2/{name}/blobs/{layer['digest']}",
                                         headers={"accept": ", ".join(CONTTYPE_IMAGE | CONTTYPE_IMAGE_GZIP)})
            with opener.open(req) as resp:
                if layer["mediaType"] in CONTTYPE_IMAGE_GZIP:
                    resp = gzip.GzipFile(fileobj=resp, mode='rb')
                for file in untar(resp):
                    basename = os.path.basename(file.path)
                    if basename.startswith(".wh."):
                        if basename == ".wh..wh..opq":
                            raise NotImplementedError("Opaque whiteouts are not implemented")
                        else:
                            basename = basename[4:]
                            print(f"Removing {os.path.join(os.path.dirname(file.path), basename)}...")
                            os.mknod(os.path.join(lay.path, 'root', os.path.dirname(file.path), basename), os.makedev(0, 0))
                    else:
                        print(f"Adding {file.path}...")
                        file.write(os.path.join(lay.path, 'root'))
        
        # Make the final layer with the configuration
        lay = Layer(name.split('/')[-1], parent=lay)
        print(f"Making {name.split('/')[-1]}...")
        cmd = []
        if isinstance(configuration['config'].get("Entrypoint"), list):
            cmd += configuration['config'].get("Entrypoint")
        if isinstance(configuration['config'].get("Cmd"), list):
            cmd += configuration['config'].get("Cmd")
        lay.cmd = cmd
        lay.env = lay.env | {v.split('=', 1)[0]: v.split('=', 1)[-1] for v in configuration['config'].get("Env", [])}
        lay.url = full_url
        lay.ephemeral = True
        lay.write()
        
        os._exit(0)
    
    # Wait for child to unshare
    os.close(w1)
    os.read(r1, 1)
    os.close(r1)

    setup_uidgidmap(pid)

    # Signal child it can continue by closing the pipe
    os.close(w2)
    os.close(r2)

    _, status = os.waitpid(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RuntimeError("Child crashed")

if __name__ == "__main__":
    #foo = pull("registry-1.docker.io/pihole/pihole:latest")
    #foo = pull("ghcr.io/home-assistant/home-assistant:stable")
    foo = pull("registry-1.docker.io/library/ubuntu:latest")
    #foo = pull("registry.fedoraproject.org/fedora:latest")