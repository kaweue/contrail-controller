#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

import uuid
import gevent
import gevent.event
import gevent.monkey
gevent.monkey.patch_all()
import requests
import copy
from cStringIO import StringIO
import bottle
import logging
import logging.handlers
from datetime import datetime
import Queue
import ConfigParser
from keystoneclient import session as ksession
from keystoneclient.auth.identity import generic as kauth
from keystoneclient import client as kclient
from keystoneclient import exceptions as kexceptions
from netaddr import *
from cfgm_common import vnc_plugin_base
from cfgm_common import utils as cfgmutils
from cfgm_common import exceptions as vnc_exc
from cfgm_common.utils import cgitb_hook
from pysandesh.sandesh_base import *
from pysandesh.sandesh_logger import *
from pysandesh.connection_info import ConnectionState
from pysandesh.gen_py.process_info.ttypes import ConnectionStatus
from pysandesh.gen_py.process_info.ttypes import ConnectionType as ConnType
from vnc_api import vnc_api
from vnc_api.gen.resource_xsd import *
from vnc_api.gen.resource_common import *

import neutron_plugin_interface as npi
from context import use_context
from neutron_plugin_db import DBInterface as npd

Q_CREATE = 'create'
Q_DELETE = 'delete'
Q_MAX_ITEMS = 1000

#Keystone SSL support
_DEFAULT_KS_CERT_BUNDLE="/tmp/keystonecertbundle.pem"

DEFAULT_SECGROUP_DESCRIPTION = "Default security group"

RETRIES_BEFORE_LOG = 100

def fill_keystone_opts(obj, conf_sections):
    obj._auth_user = conf_sections.get('KEYSTONE', 'admin_user')
    obj._auth_passwd = conf_sections.get('KEYSTONE', 'admin_password')
    obj._admin_token = conf_sections.get('KEYSTONE', 'admin_token')
    obj._admin_tenant = conf_sections.get('KEYSTONE', 'admin_tenant_name')
    try:
        obj._keystone_sync_on_demand = conf_sections.getboolean('KEYSTONE',
                                               'keystone_sync_on_demand')
    except ConfigParser.NoOptionError:
        obj._keystone_sync_on_demand = True

    try:
        obj._insecure = conf_sections.getboolean('KEYSTONE', 'insecure')
    except ConfigParser.NoOptionError:
        obj._insecure = True

    try:
        obj._certfile = conf_sections.get('KEYSTONE', 'certfile')
    except ConfigParser.NoOptionError:
        obj._certfile = ''

    try:
        obj._keyfile = conf_sections.get('KEYSTONE', 'keyfile')
    except ConfigParser.NoOptionError:
        obj._keyfile = ''

    try:
        obj._cafile= conf_sections.get('KEYSTONE', 'cafile')
    except ConfigParser.NoOptionError:
        obj._cafile = ''

    obj._kscertbundle=''
    obj._use_certs=False
    if obj._cafile:
        certs = [obj._cafile]
        if obj._keyfile and obj._certfile:
            certs=[obj._certfile,obj._keyfile,obj._cafile]
        obj._kscertbundle=cfgmutils.getCertKeyCaBundle(_DEFAULT_KS_CERT_BUNDLE,certs)
        obj._use_certs=True

    try:
        obj._auth_url = conf_sections.get('KEYSTONE', 'auth_url')
    except ConfigParser.NoOptionError:
        # deprecated knobs - for backward compat
        obj._auth_proto = conf_sections.get('KEYSTONE', 'auth_protocol')
        obj._auth_host = conf_sections.get('KEYSTONE', 'auth_host')
        obj._auth_port = conf_sections.get('KEYSTONE', 'auth_port')
        obj._auth_url = "%s://%s:%s/v2.0" % (obj._auth_proto, obj._auth_host,
                                             obj._auth_port)
    try:
        obj._err_file = conf_sections.get('DEFAULTS', 'trace_file')
    except ConfigParser.NoOptionError:
        obj._err_file = '/var/log/contrail/vnc_openstack.err'

    try:
        # Duration between polls to keystone to find deleted projects
        resync_interval = conf_sections.get('DEFAULTS',
                                            'keystone_resync_interval_secs')
    except ConfigParser.NoOptionError:
        resync_interval = '60'
    obj._resync_interval_secs = float(resync_interval)

    try:
        # Number of workers used to process keystone project resyncing
        resync_workers = conf_sections.get('DEFAULTS',
                                           'keystone_resync_workers')
    except ConfigParser.NoOptionError:
        resync_workers = '10'
    obj._resync_number_workers = int(resync_workers)

    try:
        # If new project with same name as an orphan project
        # (gone in keystone, present in # contrail with resources within)
        # is encountered,
        # a. proceed with unique ified name (new_unique_fqn)
        # b. refuse to sync (new_fail)
        # c. cascade delete (TODO)
        resync_mode = conf_sections.get('DEFAULTS',
                                        'keystone_resync_stale_mode')
    except ConfigParser.NoOptionError:
        resync_mode = 'new_unique_fqn'
    obj._resync_stale_mode = resync_mode

    try:
        # Get the domain_id for keystone v3
        obj._domain_id = conf_sections.get('KEYSTONE', 'admin_domain_id')
    except ConfigParser.NoOptionError:
        obj._domain_id = 'default'

    try:
        # Get the user_domain_name for keystone v3
        obj._user_domain_name = conf_sections.get('KEYSTONE', 'admin_user_domain_name')
    except ConfigParser.NoOptionError:
        obj._user_domain_name = 'Default'

    try:
        # Get the project_domain_name for keystone v3
        obj._project_domain_name = conf_sections.get('KEYSTONE', 'project_domain_name')
    except ConfigParser.NoOptionError:
        obj._project_domain_name = 'Default'

    try:
        # Get the project_name for keystone v3
        obj._project_name = conf_sections.get('KEYSTONE', 'project_name')
    except ConfigParser.NoOptionError:
        obj._project_name = obj._admin_tenant

    try:
        # Get the endpoint_type
        obj._endpoint_type = conf_sections.get('KEYSTONE', 'endpoint_type')
    except ConfigParser.NoOptionError:
        obj._endpoint_type = None

def _create_default_security_group(vnc_lib, proj_obj):
    def _get_rule(ingress, sg, prefix, ethertype):
        sgr_uuid = str(uuid.uuid4())
        if sg:
            addr = AddressType(
                security_group=proj_obj.get_fq_name_str() + ':' + sg)
        elif prefix:
            addr = AddressType(subnet=SubnetType(prefix, 0))
        local_addr = AddressType(security_group='local')
        if ingress:
            src_addr = addr
            dst_addr = local_addr
        else:
            src_addr = local_addr
            dst_addr = addr
        rule = PolicyRuleType(rule_uuid=sgr_uuid, direction='>',
                              protocol='any',
                              src_addresses=[src_addr],
                              src_ports=[PortType(0, 65535)],
                              dst_addresses=[dst_addr],
                              dst_ports=[PortType(0, 65535)],
                              ethertype=ethertype)
        return rule

    rules = [_get_rule(True, 'default', None, 'IPv4'),
             _get_rule(True, 'default', None, 'IPv6'),
             _get_rule(False, None, '0.0.0.0', 'IPv4'),
             _get_rule(False, None, '::', 'IPv6')]
    sg_rules = PolicyEntriesType(rules)

    # create security group
    id_perms = IdPermsType(enable=True,
                           description=DEFAULT_SECGROUP_DESCRIPTION)
    sg_obj = vnc_api.SecurityGroup(name='default', parent_obj=proj_obj,
                                   id_perms=id_perms,
                                   security_group_entries=sg_rules)

    vnc_lib.security_group_create(sg_obj)
    # neutron doesn't propagate user token
    vnc_lib.chown(sg_obj.get_uuid(), proj_obj.get_uuid())


def ensure_default_security_group(vnc_lib, proj_obj):
    sg_groups = proj_obj.get_security_groups()
    for sg_group in sg_groups or []:
        if sg_group['to'][-1] == 'default':
            return
    try:
        _create_default_security_group(vnc_lib, proj_obj)
    except vnc_api.RefsExistError:
        # Created by different worker/node
        # so we can ignore the RefsExistError exception
        pass


openstack_driver = None
class OpenstackDriver(vnc_plugin_base.Resync):
    def __init__(self, api_server_ip, api_server_port, conf_sections, sandesh):
        global openstack_driver
        openstack_driver = self
        if api_server_ip == '0.0.0.0':
            self._vnc_api_ip = '127.0.0.1'
        else:
            self._vnc_api_ip = api_server_ip

        self._vnc_api_port = api_server_port

        self._config_sections = conf_sections
        fill_keystone_opts(self, conf_sections)

        self._ks = None
        ConnectionState.update(conn_type=ConnType.OTHER, name='Keystone',
                               status=ConnectionStatus.INIT, message='',
                               server_addrs=[self._auth_url])
        self._get_keystone_conn()

        if self._ks.version == 'v3':
            self._ks_domains_list = self._ksv3_domains_list
            self._ks_domain_get = self._ksv3_domain_get
            self._ks_projects_list = self._ksv3_projects_list
            self._ks_project_get = self._ksv3_project_get
            self.sync_project_to_vnc = self._ksv3_sync_project_to_vnc
            self._add_project_to_vnc = self._ksv3_add_project_to_vnc
            self._del_project_from_vnc = self._ksv3_del_project_from_vnc
            self._vnc_default_domain_id = None
        else:
            self._ks_domains_list = None
            self._ks_domain_get = None
            self._ks_projects_list = self._ksv2_projects_list
            self._ks_project_get = self._ksv2_project_get
            self.sync_project_to_vnc = self._ksv2_sync_project_to_vnc
            self._add_project_to_vnc = self._ksv2_add_project_to_vnc
            self._del_project_from_vnc = self._ksv2_del_project_from_vnc

        self._vnc_lib = None

        # resync failures, don't retry forever
        self._failed_domain_dels = set()
        self._failed_project_dels = set()

        # active domains/projects in contrail/vnc api server
        self._vnc_domain_ids = set()
        self._vnc_project_ids = set()

        # logging
        self._sandesh_logger = sandesh.logger()
        self._vnc_os_logger = logging.getLogger(__name__)
        self._vnc_os_logger.setLevel(logging.ERROR)
        # Add the log message handler to the logger
        try:
            with open(self._err_file, 'a'):
                handler = logging.handlers.RotatingFileHandler(
                    self._err_file, maxBytes=64*1024, backupCount=5)
                self._vnc_os_logger.addHandler(handler)
        except IOError:
            self._sandesh_logger.error("Failed to open trace file %s" %
                                       self._err_file)
        self.q = Queue.Queue(maxsize=Q_MAX_ITEMS)
    #end __init__

    def _cgitb_error_log(self):
        tmp_file = StringIO()
        cgitb_hook(format="text", file=tmp_file)
        self._vnc_os_logger.error("%s" % tmp_file.getvalue())
        tmp_file.close()

    def __call__(self):
        pass
    #end __call__

    def _get_vnc_conn(self):
        if self._vnc_lib:
            return

        self._vnc_lib = vnc_api.VncApi(
            api_server_host=self._vnc_api_ip,
            api_server_port=self._vnc_api_port,
            username=self._auth_user,
            password=self._auth_passwd,
            tenant_name=self._admin_tenant)
    # end _get_vnc_conn

    def _get_keystone_conn(self):
        if self._ks:
            return

        verify = self._kscertbundle if self._use_certs else not self._insecure
        if self._admin_token:
            auth = kauth.token.Token(self._auth_url, token=self._admin_token)
        else:
            kwargs = {
                'username': self._auth_user,
                'password': self._auth_passwd,
            }
            # Add user domain info
            kwargs.update(**cfgmutils.get_user_domain_kwargs(self._config_sections))
            # Get project scope auth params
            scope_kwargs = cfgmutils.get_project_scope_kwargs(self._config_sections)
            if not scope_kwargs:
                # Default to domain scoped auth
                scope_kwargs = cfgmutils.get_domain_scope_kwargs(self._config_sections)
            kwargs.update(**scope_kwargs)
            auth = kauth.password.Password(self._auth_url, **kwargs)

        sess = ksession.Session(auth=auth, verify=verify)

        try:
            self._ks = kclient.Client(session=sess, auth_url=self._auth_url)
        except kexceptions.DiscoveryFailure:
            # Probably a v2 Keytone API, remove v3 args and try again
            v3_args = ['user_domain_name', 'project_domain_name', 'domain_id']
            for arg in v3_args:
                kwargs.pop(arg, None)
            kwargs['project_name'] = self._admin_tenant
            auth = kauth.password.Password(self._auth_url, **kwargs)
            sess = ksession.Session(auth=auth, verify=verify)
            self._ks = kclient.Client(session=sess, auth_url=self._auth_url)

        if self._endpoint_type and auth.auth_ref.service_catalog:
            self._ks.management_url = \
                auth.auth_ref.service_catalog.get_urls(
                    service_type='identity',
                    endpoint_type=self._endpoint_type)[0]

        ConnectionState.update(conn_type=ConnType.OTHER, name='Keystone',
                               status=ConnectionStatus.UP, message='',
                               server_addrs=[self._auth_url])

    def _ksv2_projects_list(self):
        return [{'id': tenant.id} for tenant in self._ks.tenants.list()]
    # end _ksv2_projects_list

    def _ksv2_project_get(self, id=None, name=None):
        # Note: under certain circumstances (if it has been initailized
        # before endpoints are populated in keystone) keystoneclient may
        # be valid to list projects, but not to read them. As it won't
        # be reset by resync_all_projects, it is reseted on error here.
        if id:
            try:
                return {'name': self._ks.tenants.get(id).name, 'id':id}
            except Exception as e:
                if self._ks is not None:
                    self._ks = None
                    ConnectionState.update(conn_type=ConnType.OTHER,
                        name='Keystone', status=ConnectionStatus.DOWN,
                        message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                        server_addrs=[self._auth_url])
                self._get_keystone_conn()
                return {'name': self._ks.tenants.get(id).name, 'id':id}
        else:
            id = None
            for tenant in self._ks.tenants.list():
                if tenant.name == name:
                    id = tenant.id
                    break
            return {'name':name, 'id':id}
    # end _ksv2_project_get

    def _ksv2_sync_project_to_vnc(self, id=None):
        self._get_keystone_conn()
        self._get_vnc_conn()
        ks_project = self._ks_project_get(id=id.replace('-', ''))
        display_name = ks_project['name']
        proj_name = display_name

        # if earlier project exists with same name but diff id,
        # create with uniqified fq_name
        fq_name = ['default-domain', display_name]
        try:
            old_id = self._vnc_lib.fq_name_to_id('project', fq_name)
            if old_id == id:
                self._vnc_project_ids.add(id)
                return
            # Project might have been quickly deleted + added.
            # Since project delete sync happens only in timer(polling),
            # try deleting old one synchronously. If delete fails due
            # to resources being present in project, proceed/fail
            # based on configuration
            try:
                self._vnc_lib.project_delete(fq_name=fq_name)
            except vnc_api.NoIdError:
                pass
            except vnc_api.RefsExistError:
                if self._resync_stale_mode == 'new_unique_fqn':
                    proj_name = '%s-%s' %(display_name, str(uuid.uuid4()))
                else:
                    errmsg = "Old project %s fqn %s exists and not empty" %(
                        old_id, fq_name)
                    self._sandesh_logger.error(errmsg)
                    raise Exception(errmsg)
        except vnc_api.NoIdError:
            pass

        proj_obj = vnc_api.Project(proj_name)
        proj_obj.display_name = display_name
        proj_obj.uuid = id
        self._vnc_lib.project_create(proj_obj)
        self._vnc_project_ids.add(id)
    # end _ksv2_sync_project_to_vnc

    def _ksv2_add_project_to_vnc(self, project_id):
        try:
            self._vnc_lib.project_read(id=project_id)
            # project exists, no-op for now,
            # sync any attr changes in future
        except vnc_api.NoIdError:
            self._ksv2_sync_project_to_vnc(project_id)
    # _ksv2_add_project_to_vnc

    def _ksv2_del_project_from_vnc(self, project_id):
        if project_id in self._failed_project_dels:
            return

        try:
            self._vnc_lib.project_delete(id=project_id)
        except vnc_api.NoIdError:
            pass
        except Exception as e:
            self._cgitb_error_log()
            self._sandesh_logger.error("Failed to delete project %s: %s" %
                                       (project_id, e))
            self._failed_project_dels.add(project_id)
    # _ksv2_del_project_from_vnc

    def _ksv3_domains_list(self):
        return [{'id': domain.id} for domain in self._ks.domains.list()]
    # end _ksv3_domains_list

    def _ksv3_domain_id_to_uuid(self, domain_id):
        if domain_id == 'default':
            return self._vnc_default_domain_id

        return str(uuid.UUID(domain_id))
    # _ksv3_domain_id_to_uuid

    def _ksv3_domain_get(self, id=None):
        try:
            return {'name': self._ks.domains.get(id).name}
        except Exception as e:
            if self._ks is not None:
                self._ks = None
                ConnectionState.update(conn_type=ConnType.OTHER,
                    name='Keystone', status=ConnectionStatus.DOWN,
                    message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                    server_addrs=[self._auth_url])
            self._get_keystone_conn()
            return {'name': self._ks.domains.get(id).name}
    # end _ksv3_domain_get

    def _ksv3_projects_list(self):
        return [{'id': project.id} for project in self._ks.projects.list()]
    # end _ksv3_projects_list

    def _ksv3_project_get(self, id=None, name=None):
        if id:
            try:
                project = self._ks.projects.get(id)
                return {'id': project.id, 'name': project.name, 'domain_id': project.domain_id}
            except Exception as e:
                if self._ks is not None:
                    self._ks = None
                    ConnectionState.update(conn_type=ConnType.OTHER,
                        name='Keystone', status=ConnectionStatus.DOWN,
                        message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                        server_addrs=[self._auth_url])
                self._get_keystone_conn()
                project = self._ks.projects.get(id)
                return {'id': project.id, 'name': project.name, 'domain_id': project.domain_id}
        else:
            id = None
            for tenant in self._ks.projects.list():
                if tenant.name == name:
                    id = tenant.id
                    break
            return {'name':name, 'id':id}

    # end _ksv3_project_get

    def _ksv3_sync_project_to_vnc(self, id=None, name=None):
        self._get_keystone_conn()
        self._get_vnc_conn()
        if id:
            ks_project = \
                self._ks_project_get(id=id.replace('-', ''))
            display_name = ks_project['name']
            project_id = id
        elif name:
            ks_project = \
                self._ks_project_get(name=name)
            project_id = ks_project['id']
            display_name = name

        domain_uuid = self._ksv3_domain_id_to_uuid(ks_project['domain_id'])
        dom_obj = self._vnc_lib.domain_read(id=domain_uuid)

        # if earlier project exists with same name but diff id,
        # create with uniqified fq_name
        fq_name = dom_obj.get_fq_name() + [display_name]
        project_name = display_name
        try:
            old_id = self._vnc_lib.fq_name_to_id('project', fq_name)
            if old_id == project_id:
                self._vnc_project_ids.add(project_id)
                return
            # Project might have been quickly deleted + added.
            # Since project delete sync happens only in timer(polling),
            # try deleting old one synchronously. If delete fails due
            # to resources being present in project, proceed/fail
            # based on configuration
            try:
                self._vnc_lib.project_delete(fq_name=fq_name)
            except vnc_api.NoIdError:
                pass
            except vnc_api.RefsExistError:
                if self._resync_stale_mode == 'new_unique_fqn':
                    project_name = '%s-%s' %(display_name, str(uuid.uuid4()))
                else:
                    errmsg = "Old project %s fqn %s exists and not empty" %(
                        old_id, fq_name)
                    self._sandesh_logger.error(errmsg)
                    raise Exception(errmsg)
        except vnc_api.NoIdError:
            pass

        proj_obj = vnc_api.Project(project_name, parent_obj=dom_obj)
        proj_obj.display_name = display_name
        proj_obj.uuid = project_id
        self._vnc_lib.project_create(proj_obj)
        self._vnc_domain_ids.add(domain_uuid)
        self._vnc_project_ids.add(project_id)
    # end _ksv3_sync_project_to_vnc

    def _ksv3_add_project_to_vnc(self, project_id):
        try:
            self._vnc_lib.project_read(id=project_id)
            # project exists, no-op for now,
            # sync any attr changes in future
        except vnc_api.NoIdError:
            self._ksv3_sync_project_to_vnc(id=project_id)
    # _ksv3_add_project_to_vnc

    def _ksv3_del_project_from_vnc(self, project_id):
        if project_id in self._failed_project_dels:
            return

        try:
            self._vnc_lib.project_delete(id=project_id)
        except vnc_api.NoIdError:
            pass
        except Exception as e:
            self._cgitb_error_log()
            self._sandesh_logger.error("Failed to delete project %s "
                                       "from vnc: %s" % (project_id, e))
            self._failed_project_dels.add(project_id)
    # _ksv3_del_project_from_vnc

    def sync_domain_to_vnc(self, domain_id):
        self._get_keystone_conn()
        self._get_vnc_conn()
        ks_domain = \
            self._ks_domain_get(domain_id.replace('-', ''))
        display_name = ks_domain['name']
        domain_name = display_name

        # if earlier domain exists with same name but diff id,
        # create with uniqified fq_name
        fq_name = [display_name]
        try:
            old_id = self._vnc_lib.fq_name_to_id('domain', fq_name)
            if domain_id == old_id:
                self._vnc_domain_ids.add(domain_id)
                return

            # Domain might have been quickly deleted + added.
            # Since domain delete sync happens only in timer(polling),
            # try deleting old one synchronously. If delete fails due
            # to resources being present in domain, proceed/fail
            # based on configuration
            try:
                self._vnc_lib.domain_delete(fq_name=fq_name)
            except vnc_api.NoIdError:
                pass
            except vnc_api.RefsExistError:
                if self._resync_stale_mode == 'new_unique_fqn':
                    domain_name = '%s-%s' %(display_name, str(uuid.uuid4()))
                else:
                    errmsg = "Old domain %s fqn %s exists and not empty" %(
                        old_id, fq_name)
                    self._sandesh_logger.error(errmsg)
                    raise Exception(errmsg)
        except vnc_api.NoIdError:
            pass

        dom_obj = vnc_api.Domain(domain_name)
        dom_obj.display_name = display_name
        dom_obj.uuid = domain_id
        self._vnc_lib.domain_create(dom_obj)
        self._vnc_domain_ids.add(domain_id)
    # sync_domain_to_vnc

    def _add_domain_to_vnc(self, domain_id):
        try:
            self._vnc_lib.domain_read(id=domain_id)
            # domain exists, no-op for now,
            # sync any attr changes in future
        except vnc_api.NoIdError:
            self.sync_domain_to_vnc(domain_id)
    # _add_domain_to_vnc

    def _del_domain_from_vnc(self, domain_id):
        if domain_id in self._failed_domain_dels:
            return

        try:
            self._vnc_lib.domain_delete(id=domain_id)
        except vnc_api.NoIdError:
            pass
        except Exception as e:
            self._sandesh_logger.error("Failed to delete domain %s "
                                       "from vnc: %s" % (domain_id, e))
            self._cgitb_error_log()
            self._failed_domain_dels.add(domain_id)
    # _del_domain_from_vnc

    def _resync_all_domains(self):
        if not self._ks_domains_list:
            # < keystonev3, no domains
            return False

        self._get_keystone_conn()
        # compare new and old set,
        # optimize for common case where nothing has changed,
        # so track the project-ids in a set add '-',
        # keystone gives uuid without...
        try:
            # The Default domain in ks(for v2 support) has id of 'default'
            # replace with uuid of default-domain in vnc
            ks_domain_ids = set(
                [str(uuid.UUID(dom['id']))
                    for dom in self._ks_domains_list() if dom['id'] != 'default'])
            ks_domain_ids.add(self._vnc_default_domain_id)
        except Exception as e:
            if self._ks is not None:
                self._ks = None
                ConnectionState.update(conn_type=ConnType.OTHER,
                    name='Keystone', status=ConnectionStatus.DOWN,
                    message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                    server_addrs=[self._auth_url])
            return True # retry

        vnc_domain_ids = self._vnc_domain_ids
        if vnc_domain_ids == ks_domain_ids:
            # no change, go back to poll
            return False

        for vnc_domain_id in vnc_domain_ids - ks_domain_ids:
            self.q.put((Q_DELETE, 'domain', vnc_domain_id))

        if self._keystone_sync_on_demand:
            # pre_domain_read will get it
            pass
        else:
            for ks_domain_id in ks_domain_ids - vnc_domain_ids:
                self.q.put((Q_CREATE, 'domain', ks_domain_id))

        self.q.join()
        gevent.sleep(0)

        # we are in sync
        self._vnc_domain_ids = ks_domain_ids

        return False
    # end _resync_all_domains

    def _resync_all_projects(self):
        self._get_keystone_conn()
        # compare new and old set,
        # optimize for common case where nothing has changed,
        # so track the project-ids in a set add '-',
        # keystone gives uuid without...
        try:
            ks_project_ids = set(
                [str(uuid.UUID(proj['id']))
                    for proj in self._ks_projects_list()])
        except Exception as e:
            if self._ks is not None:
                self._ks = None
                ConnectionState.update(conn_type=ConnType.OTHER,
                    name='Keystone', status=ConnectionStatus.DOWN,
                    message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                    server_addrs=[self._auth_url])
            return True # retry

        vnc_project_ids = self._vnc_project_ids
        if vnc_project_ids == ks_project_ids:
            # no change, go back to poll
            return False

        for vnc_project_id in vnc_project_ids - ks_project_ids:
            self.q.put((Q_DELETE, 'project', vnc_project_id))

        if self._keystone_sync_on_demand:
            pass # pre_project_read will get it
        else:
            for ks_project_id in ks_project_ids - vnc_project_ids:
                self.q.put((Q_CREATE, 'project', ks_project_id))

        self.q.join()
        gevent.sleep(0)
        # we are in sync
        self._vnc_project_ids = ks_project_ids

        return False
    # end _resync_all_projects

    def _resync_domains_projects_forever(self):
        try:
            # get connection to api-server REST interface
            while True:
                try:
                    self._get_vnc_conn()
                    break
                except requests.ConnectionError:
                    gevent.sleep(1)

            vnc_domains = self._vnc_lib.domains_list()['domains']
            for dom in vnc_domains:
                self._vnc_domain_ids.add(dom['uuid'])
                if dom['fq_name'] == ['default-domain']:
                    self._vnc_default_domain_id = dom['uuid']

            vnc_all_projects = self._vnc_lib.projects_list()['projects']
            # remove default-domain:default-project from audit list
            default_proj_fq_name = ['default-domain', 'default-project']
            vnc_project_ids = set([proj['uuid'] for proj in vnc_all_projects
                                 if proj['fq_name'] != default_proj_fq_name])
            self._vnc_project_ids = vnc_project_ids
        except Exception as e:
            self._cgitb_error_log()
            self._sandesh_logger.error(
                "Connection to API server failed: %s" %  e)

        while True:
            # Get domains/projects from Keystone and audit with api-server
            try:
                retry = self._resync_all_domains()
                if retry:
                    gevent.sleep(self._resync_interval_secs)
                    continue
            except Exception as e:
                if self._ks is not None:
                    self._ks = None
                    ConnectionState.update(conn_type=ConnType.OTHER,
                        name='Keystone', status=ConnectionStatus.DOWN,
                        message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                        server_addrs=[self._auth_url])
                    self._cgitb_error_log()
                    self._sandesh_logger.error(
                        "Failed to resync domains: %s" %  e)

            try:
                retry = self._resync_all_projects()
                if retry:
                    gevent.sleep(self._resync_interval_secs)
                    continue
            except Exception as e:
                if self._ks is not None:
                    self._ks = None
                    ConnectionState.update(conn_type=ConnType.OTHER,
                        name='Keystone', status=ConnectionStatus.DOWN,
                        message='Error: %s at UTC %s' %(e, datetime.utcnow()),
                        server_addrs=[self._auth_url])
                    self._cgitb_error_log()
                    self._sandesh_logger.error(
                        "Failed to resync projects: %s" %  e)

            gevent.sleep(self._resync_interval_secs)

        #end while True

    #end _resync_domains_projects_forever

    def resync_domains_projects(self):
        # add asynchronously
        self._main_glet = gevent.spawn(self._resync_domains_projects_forever)
        self._worker_glets = []
        for x in range(self._resync_number_workers):
            self._worker_glets.append(gevent.spawn(self._resync_worker))
    #end resync_domains_projects

    def _resync_worker(self):
        while True:
            oper, obj_type, obj_id = self.q.get()
            try:
                if oper == Q_DELETE:
                    if obj_type == 'domain':
                        self._del_domain_from_vnc(obj_id)
                    elif obj_type == 'project':
                        self._del_project_from_vnc(obj_id)
                    else:
                        raise KeyError("An invalid obj_type was specified: %s",
                                        obj_type)
                elif oper == Q_CREATE:
                    if obj_type == 'domain':
                        self._add_domain_to_vnc(obj_id)
                    elif obj_type == 'project':
                        self._add_project_to_vnc(obj_id)
                    else:
                        raise KeyError("An invalid obj_type was specified: %s",
                                        obj_type)
                else:
                    raise KeyError("An invalid operation was specified: %s", oper)
            except (ValueError, KeyError, Exception):
                # For an unpack error or and invalid kind.
                self.log_exception()
            finally:
                self.q.task_done()
    # end _resync_worker

#end class OpenstackResync


class ResourceApiDriver(vnc_plugin_base.ResourceApi):
    def __init__(self, api_server_ip, api_server_port, conf_sections, sandesh,
                 propagate_map_exceptions=False):
        if api_server_ip == '0.0.0.0':
            self._vnc_api_ip = '127.0.0.1'
        else:
            self._vnc_api_ip = api_server_ip
        self._sandesh_logger = sandesh.logger()
        self._vnc_api_port = api_server_port
        self._config_sections = conf_sections
        fill_keystone_opts(self, conf_sections)

        self._vnc_lib = None
        self._openstack_drv = openstack_driver
        self._connected_to_api_server = gevent.event.Event()
        self._conn_glet = gevent.spawn(self._get_api_connection)
    # end __init__

    def _get_api_connection(self):
        if self._vnc_lib:
            return

        # get connection to api-server REST interface
        tries = 0
        while True:
            try:
                tries = tries + 1
                self._vnc_lib = vnc_api.VncApi(
                    api_server_host=self._vnc_api_ip,
                    api_server_port=self._vnc_api_port,
                    username=self._auth_user,
                    password=self._auth_passwd,
                    tenant_name=self._admin_tenant)
                self._connected_to_api_server.set()

                vnc_lib = self._vnc_lib
                domain_id = vnc_lib.fq_name_to_id(
                        'domain', ['default-domain'])
                project_id = vnc_lib.fq_name_to_id(
                        'project', ['default-domain', 'default-project'])
                break
            except Exception as e:
                if tries % RETRIES_BEFORE_LOG == 0:
                    err_msg = "Connect error to contrail api %s tries: %s" \
                              %(tries, e)
                    self._sandesh_logger.error(err_msg)
                gevent.sleep(1)
    # end _get_api_connection

    def __call__(self):
        pass
    #end __call__

    def _create_default_security_group(self, proj_dict):
        proj_obj = vnc_api.Project.from_dict(**proj_dict)
        ensure_default_security_group(self._vnc_lib, proj_obj)
    # end _create_default_security_group

    def wait_for_api_server_connection(func):
        def wrapper(self, *args, **kwargs):
            self._connected_to_api_server.wait()
            return func(self, *args, **kwargs)

        return wrapper
    # end wait_for_api_server_connection

    @wait_for_api_server_connection
    def pre_domain_read(self, id):
        if not self._keystone_sync_on_demand:
            # domain added via poll
            return

        # use list instead of read as read will be recursive
        # leading us back here!
        dom_list = self._vnc_lib.domains_list(obj_uuids=[id])
        if len(dom_list['domains']) == 1:
            # read succeeded domain already known, done.
            return

        # follow through, and sync domain to contrail
        try:
            self._openstack_drv.sync_domain_to_vnc(id)
        except vnc_api.RefsExistError as e:
            # another api server has brought syncd it
            pass
    # end pre_domain_read
    @wait_for_api_server_connection
    def pre_project_read_fqname(self, fq_name):
        if not self._keystone_sync_on_demand or fq_name == None:
            # project added via poll
            return
        name = fq_name[-1]
        id = self._openstack_drv._ks_project_get(id=None, name=name).get('id')
        if id:
            proj_obj = vnc_api.Project(name)
            proj_obj.fq_name = list(fq_name)
            proj_obj.display_name = name
            proj_obj.uuid = str(uuid.UUID(id))
            try:
                self._vnc_lib.project_create(proj_obj)
            except RefsExistError as e:
                pass
            self._openstack_drv._vnc_project_ids.add(id)
        else:
            raise Exception('project %s not presend in keystone' %(name))

    @wait_for_api_server_connection
    def pre_project_read(self, id):
        if not self._keystone_sync_on_demand:
            # project added via poll
            return

        # use list instead of read as read will be recursive
        # leading us back here!
        proj_list = self._vnc_lib.projects_list(obj_uuids=[id])
        if len(proj_list['projects']) == 1:
            # read succeeded project already known, done.
            return

        # follow through, and sync project to contrail
        try:
            self._openstack_drv.sync_project_to_vnc(id)
        except vnc_api.RefsExistError as e:
            # another api server has brought syncd it
            pass
    # end pre_project_read

    @wait_for_api_server_connection
    def post_project_create(self, proj_dict):
        self._create_default_security_group(proj_dict)
    # end post_create_project

    @wait_for_api_server_connection
    def pre_project_delete(self, proj_uuid):
        try:
            proj_obj = self._vnc_lib.project_read(id=proj_uuid)
        except vnc_exc.NoIdError:
            # another api server has brought that project deletion
            return
        sec_groups = proj_obj.get_security_groups()
        for group in sec_groups or []:
            if group['to'][2] == 'default':
                try:
                    # another api server has brought that project deletion and
                    # its default security group
                    self._vnc_lib.security_group_delete(id=group['uuid'])
                except vnc_exc.NoIdError:
                    pass
                return
    # end pre_project_delete

    @wait_for_api_server_connection
    def pre_virtual_network_create(self, vn_dict):
        pass
    # end pre_virtual_network_create

    def _update_subnet_id(self, vn_uuid, new_refs, old_refs):
        def get_subnets(ipam_refs):
            subnets = {}
            if ipam_refs:
                for ipam_ref in ipam_refs:
                    vnsn_data = ipam_ref['attr']
                    ipam_subnets = vnsn_data['ipam_subnets']
                    for ipam_subnet in ipam_subnets:
                        if 'subnet' in ipam_subnet:
                            subnet_dict = copy.deepcopy(ipam_subnet['subnet'])
                            prefix = subnet_dict['ip_prefix']
                            prefix_len = subnet_dict['ip_prefix_len']
                        else:
                            #flat subnet where, subnet-uuid is unique,
                            #representing all subnets on ipam
                            prefix = '0.0.0.0'
                            prefix_len = 0
                        network = IPNetwork('%s/%s' % (prefix, prefix_len))
                        subnet_name = vn_uuid + ' ' + str(network)
                        subnet_uuid = ipam_subnet['subnet_uuid']
                        subnets[subnet_uuid] = subnet_name
            return subnets

        new_subnets = get_subnets(new_refs)
        existing_subnets = get_subnets(old_refs)

        add_subnets = set(new_subnets.keys()) - set(existing_subnets.keys())
        del_subnets = set(existing_subnets.keys()) - set(new_subnets.keys())
        for subnet in del_subnets or []:
            try:
                self._vnc_lib.kv_delete(existing_subnets[subnet])
            except NoIdError:
                pass
            self._vnc_lib.kv_delete(subnet)
        for subnet in add_subnets or []:
            self._vnc_lib.kv_store(subnet, new_subnets[subnet])

    @wait_for_api_server_connection
    def post_virtual_network_create(self, vn_dict):
        self._update_subnet_id(vn_dict['uuid'], vn_dict.get('network_ipam_refs'), None)
    # end post_virtual_network_create

    @wait_for_api_server_connection
    def post_virtual_network_update(self, vn_uuid, vn_dict, old_dict):
        ipam_refs = vn_dict.get('network_ipam_refs')
        if ipam_refs is None:
            return
        self._update_subnet_id(vn_uuid, vn_dict.get('network_ipam_refs'),
                              old_dict.get('network_ipam_refs'))
    # end post_virtual_network_update

    @wait_for_api_server_connection
    def post_virtual_network_delete(self, vn_uuid, vn_dict):
        self._update_subnet_id(vn_uuid, None, vn_dict.get('network_ipam_refs'))
    # end post_virtual_network_delete

# end class ResourceApiDriver

class NeutronApiDriver(vnc_plugin_base.NeutronApi):
    def __init__(self, api_server_ip, api_server_port, conf_sections, sandesh, **kwargs):
        self._logger = sandesh.logger()
        self.api_server_obj = kwargs.get('api_server_obj')
        self._npi = npi.NeutronPluginInterface(api_server_ip, api_server_port,
            conf_sections, sandesh, api_server_obj=self.api_server_obj)

        # Bottle callbacks for network operations
        self.route('/neutron/network',
                     'POST', self._npi.plugin_http_post_network)

        # Bottle callbacks for subnet operations
        self.route('/neutron/subnet',
                     'POST', self._npi.plugin_http_post_subnet)

        # Bottle callbacks for port operations
        self.route('/neutron/port',
                     'POST', self._npi.plugin_http_post_port)

        # Bottle callbacks for floating IP operations
        self.route('/neutron/floatingip',
                     'POST', self._npi.plugin_http_post_floatingip)

        # Bottle callbacks for security group operations
        self.route('/neutron/security_group',
                     'POST', self._npi.plugin_http_post_securitygroup)

        # Bottle callbacks for security group rule operations
        self.route('/neutron/security_group_rule',
                     'POST', self._npi.plugin_http_post_securitygrouprule)

        # Bottle callbacks for router operations
        self.route('/neutron/router',
                     'POST', self._npi.plugin_http_post_router)

        # Bottle callbacks for ipam operations
        self.route('/neutron/ipam',
                     'POST', self._npi.plugin_http_post_ipam)

        # Bottle callbacks for Policy operations
        self.route('/neutron/policy',
                     'POST', self._npi.plugin_http_post_policy)

        # Bottle callbacks for route-table operations
        self.route('/neutron/route_table',
                     'POST', self._npi.plugin_http_post_route_table)

        # Bottle callbacks for svc-instance operations
        self.route('/neutron/nat_instance',
                     'POST', self._npi.plugin_http_post_svc_instance)

        # Bottle callbacks for virtual-router operations
        self.route('/neutron/virtual_router',
                     'POST', self._npi.plugin_http_post_virtual_router)

    def route(self, uri, method, handler):
        @use_context
        def handler_trap_exception(*args, **kwargs):
            try:
                response = handler(*args, **kwargs)
                return response
            except vnc_exc.AuthFailed as e:
                bottle.abort(401, str(e))
            except vnc_exc.PermissionDenied as e:
                npd._raise_contrail_exception(
                          'NotAuthorized', msg=str(e))
            except vnc_exc.BadRequest as e:
                npd._raise_contrail_exception(
                          'BadRequest', msg=str(e))
            except vnc_exc.RefsExistError as e:
                npd._raise_contrail_exception(
                          'Conflict', msg=str(e))
            except vnc_exc.OverQuota as e:
                npd._raise_contrail_exception(
                          'OverQuota', msg=str(e))
            except vnc_exc.NoIdError as e:
                npd._raise_contrail_exception(
                          'NotFound', msg=str(e))
            except Exception as e:
                # don't log details of bottle.abort i.e handled error cases
                if not isinstance(e, bottle.HTTPError):
                    string_buf = StringIO()
                    cgitb_hook(file=string_buf, format="text",)
                    err_msg = string_buf.getvalue()
                    self._logger.error(err_msg)

                raise

        self.api_server_obj.api_bottle.route(uri, method, handler_trap_exception)

    def __call__(self):
        pass
