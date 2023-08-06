from typing import Dict

class ConfigError(Exception):
    """Invalid configuration file passed"""

def load_config(file: str) -> Dict:
    """Load configuration from the text file."""

    ret = {}

    with open(file) as fp:
        section = ''
        for line in fp:
            line = line.strip()
            if len(line) == 0 or line[0] == '#' or line[0] == ';':
                # It is an empty line or a comment
                pass
            elif line[0] == '[' or line[-1] == ']':
                section = line[1:-1]
                if section in ret.keys() and not isinstance(ret[section], dict):
                    raise ConfigError(f"[{section}] already present as regular key")
                if not section in ret.keys():
                    ret[section] = {}
            elif '=' in line:
                key, value = (s.strip() for s in line.split('=', 1))

                # Figure out where to store this value
                if section:
                    ret_section = ret[section]
                else:
                    ret_section = ret
                
                ret_section[key] = value
            else:
                raise ConfigError(f"Could not parse the line '{line}'")
    return ret

def write_config(file: str, config: Dict):
    """Write the provided configuration to the text file."""

    with open(file, "w") as fp:
        for key, value in config.items():
            if not isinstance(value, dict):
                print(f"{key} = {value}", file=fp)

        for section, subconfig in config.items():
            if isinstance(subconfig, dict):
                print(f"\n[{section}]", file=fp)
                for key, value in subconfig.items():
                    print(f"{key} = {value}", file=fp)