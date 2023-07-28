import gzip
import os
import re
from typing import Optional
import requests
import requests.auth
import urllib3
from layer import Layer, setup_uidgidmap

import linux
from untar import untar

class BearerAuthError(Exception):
    pass

class PullError(Exception):
    pass

class BearerAuth(requests.auth.AuthBase):
    token: str

    def __init__(self, token: Optional[str] = None):
        self.token = token
    
    def __call__(self, r: requests.PreparedRequest):
        if self.token is not None:
            r.headers["authorization"] = f"Bearer {self.token}"
        r.register_hook("response", self.handle_response)

        return r
    
    def handle_response(self, r: requests.Response, **kwargs):
        if r.status_code == 401:
            # Parse the authentication header
            pat = lambda name: f"{name}=\"(?P<{name}>[^\"]+)\",?"
            m: Optional[re.Match] = re.fullmatch(f"Bearer ({pat('realm')}|{pat('service')}|{pat('scope')})+", r.headers["www-authenticate"])
            if m:
                auth = m.groupdict()
            else:
                raise BearerAuthError(f"Could not parse www-authenticate header: {r.headers['www-authenticate']}")

            # Obtain a token from whatever URL is indicated in the realm
            authresponse = requests.get(auth['realm'], {'service': auth['service'], 'scope': auth['scope']})
            authresponse.raise_for_status()

            # Store the token in the object for future usage
            self.token = authresponse.json()["token"]

            # Redo the request with the authorization header and without this hook (in case it again returns 401)
            r2 = r.request.copy()
            r2.headers["authorization"] = f"Bearer {self.token}"
            r2.deregister_hook("response", self.handle_response)
            return requests.Session().send(r2)

def pull(url: str, *, auth: Optional[BearerAuth] = None):
    if auth is None:
        auth = BearerAuth()
    
    (url, rest) = url.split("/", 1)
    (name, reference) = rest.split(":", 1)

    # First get a list of manifests to find the one belonging to this architecture
    print("Retrieving list of manifests...")
    r = requests.get(f"https://{url}/v2/{name}/manifests/{reference}",
                        auth=auth,
                        headers={"accept": "application/vnd.docker.distribution.manifest.list.v2+json"})
    r.raise_for_status()
    manifest_list = r.json()

    # Find the manifest belonging to this architecture
    for manifest in manifest_list['manifests']:
        if manifest['platform']['architecture'] == linux.ARCH and manifest['platform'].get('variant', linux.VARIANT) == linux.VARIANT and manifest['platform']['os'] == linux.OS:
            print("Retrieving manifest...")
            r = requests.get(f"https://{url}/v2/{name}/manifests/{manifest['digest']}",
                                auth=auth,
                                headers={"accept": manifest['mediaType']})
            r.raise_for_status()
            manifest = r.json()
            break
    else:
        archlist = [manifest['platform']['architecture'] + manifest['platform'].get('variant', '') for manifest in manifest_list['manifests']]
        raise PullError(f"Architecture {linux.ARCH} not present; found {', '.join(archlist)}")
    
    # Pull the configuration. There is one major difference between how OCI
    # treats images and how we do it: in OCI, there is only a single
    # configuration for all layers as a whole. Hence, we pull the configuration
    # and put that on the *last* layer.
    print("Retrieving configuration...")
    r = requests.get(f"https://{url}/v2/{name}/blobs/{manifest['config']['digest']}",
                     auth=auth,
                     headers={"accept": manifest['config']['mediaType']})
    r.raise_for_status()
    configuration = r.json()

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
            with requests.get(f"https://{url}/v2/{name}/blobs/{layer['digest']}",
                            auth=auth,
                            headers={"accept": layer["mediaType"]},
                            stream=True) as r:
                r.raise_for_status()
                fp: urllib3.response.HTTPResponse = r.raw
                fp.decode_content = True
                if "gzip" in layer["mediaType"]:
                    fp = gzip.GzipFile(fileobj=fp, mode='rb')
                for file in untar(fp):
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

    # Signal child to unshare by closing the pipe
    os.close(w2)
    os.close(r2)

    os.wait()

    return configuration

if __name__ == "__main__":
    foo = pull("registry-1.docker.io/pihole/pihole:latest")
    #foo = pull("ghcr.io/home-assistant/home-assistant:stable")