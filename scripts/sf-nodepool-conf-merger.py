#!/usr/bin/env python

import os
import sys
import yaml


def merge(_nodepool):
    conf = yaml.safe_load(open(_nodepool))
    user = yaml.safe_load(open("nodepool/nodepool.yaml"))

    for provider in user['providers']:
        for image in provider['images']:
            image['private-key'] = '/var/lib/nodepool/.ssh/id_rsa'
        if not provider.get("image-type"):
            provider["image-type"] = "raw"

    for dib in user['diskimages']:
        envvars = dib.setdefault('env-vars', {})
        envvars['TMPDIR'] = '/var/cache/nodepool/dib_tmp'
        envvars['DIB_IMAGE_CACHE'] = '/var/cache/nodepool/dib_cache'
        envvars['DIB_GRUB_TIMEOUT'] = '0'
        envvars['DIB_CHECKSUM'] = '1'
        # Make sure env-vars are str
        for k, v in envvars.items():
            if not isinstance(v, basestring):
                envvars[k] = str(v)

    if 'cron' in user:
        conf['cron'] = user['cron']
    conf['labels'] = user['labels']
    conf['providers'] = user['providers']
    conf['diskimages'] = user['diskimages']
    return yaml.dump(conf, default_flow_style=False)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print "Please provide the target filename"
        sys.exit(1)
    out = sys.argv[1]
    _nodepool = "%s/_nodepool.yaml" % os.path.dirname(out)
    for reqfile in (_nodepool, "nodepool/nodepool.yaml"):
        if not os.path.isfile(reqfile):
            print "%s: missing file" % reqfile
            sys.exit(1)
    merged = merge(_nodepool)
    open(out, 'w').write(merged)