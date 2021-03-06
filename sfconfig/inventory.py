# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import configparser
import copy
import os
import yaml

from jinja2 import FileSystemLoader
from jinja2.environment import Environment

import sfconfig.utils


def write_playbook(playbook_path, playbook):
    if os.path.exists(playbook_path):
        current = open(playbook_path).read()
    else:
        current = ""
    new = yaml.safe_dump(playbook, default_flow_style=False)
    if new != current:
        with open(playbook_path, "w") as out:
            out.write(new)
        print("[+] Wrote %s" % playbook_path)


def notify_journald(msg):
    return {'name': 'Signal journald', 'command': 'logger sfconfig %s' % msg}


def host_play(host, roles=[], params={}, tasks=[]):
    """Generate a ansible host play block"""
    if not isinstance(host, dict):
        host = {'hostname': host}
    if not isinstance(roles, list):
        roles = [roles]
    if not isinstance(tasks, list):
        tasks = [tasks]

    host_play = {'hosts': host['hostname'], 'any_errors_fatal': True}
    if roles:
        host_play['roles'] = []
        for role in roles:
            host_play['roles'].append('sf-%s' % role)
    if tasks:
        host_play['tasks'] = tasks
    if host.get('params', None):
        host_play['vars'] = copy.deepcopy(host['params'])
    if params:
        host_play.setdefault('vars', {}).update(params)
    return host_play


def disable(args, pb):
    action = {'role_action': 'disable', 'erase': False}

    # Disable all but mysql and install-server
    for host in args.inventory:
        host_roles = [role for role in host["roles"] if
                      role not in ("mysql", "install-server")]
        pb.append(host_play(host, host_roles, action))

    # Then disable mysql and install-server
    pb.append(host_play('mysql', 'mysql', action))
    pb.append(host_play('install-server', 'install-server', action))

    # Last disable base from all
    pb.append(host_play('all', 'base', action))


def erase(args, pb):
    action = {'role_action': 'disable', 'erase': True}

    # First get confirmation
    prompt = "WARNING: this playbook will *DESTROY* software factory " \
             "data , type the fqdn to continue or CTRL-C to abort"
    pb.append(host_play('install-server', tasks={
        'pause': {'prompt': prompt},
        'when': 'sfconfig_batch is not defined',
        'register': 'erase_prompt'}))
    pb.append(host_play('install-server', tasks={
        'fail': {'msg': 'Incorrect hostname'},
        'when': ['sfconfig_batch is not defined',
                 'erase_prompt.user_input != fqdn']}))

    # Erase all but mysql and install-server
    for host in args.inventory:
        host_roles = [role for role in host["roles"] if
                      role not in ("mysql", "install-server")]
        pb.append(host_play(host, host_roles, action))

    # Then erase mysql and install-server
    pb.append(host_play('mysql', 'mysql', action))
    pb.append(host_play('install-server', 'install-server', action))

    # Last erase base from all
    pb.append(host_play('all', 'base', action))


def disable_action(args):
    pb = []
    playbook_name = "sfconfig"
    if args.erase:
        playbook_name += "_erase"
        erase(args, pb)
    elif args.disable:
        playbook_name += "_disable"
        disable(args, pb)
    return playbook_name, pb


def update(args, pb):
    # Ensure mirrors are set before upgrade
    for host in args.inventory:
        pb.append(host_play(host, host["roles"],
                            {'role_action': 'configure_mirror'}))

    # Apply upgrade role on all hosts to update packages
    pb.append(host_play('all', 'upgrade'))

    # Update config repositories
    pb.append(host_play('install-server', 'repos', {'role_action': 'update'}))


def install(args, pb):
    action = {'role_action': 'install'}
    pb.append(host_play('all', 'base', action))
    for host in args.inventory:
        pb.append(host_play(host, host['roles'], action))


def recover(args, pb):
    pb.append(host_play('install-server', tasks=[
        notify_journald("recover started"),
        {'name': 'Ensure role directory exists',
         'file': {
             'path': '/var/lib/software-factory/backup/{{ item }}',
             'state': 'directory'
         },
         'loop': [role for role in args.glue['roles']]}
    ]))

    # Start mysql
    pb.append(host_play('mysql', tasks=[
        {'yum': {
            'name': 'mariadb-server',
            'state': 'present',
            'disablerepo': '{{ yum_disable_repo|default(omit) }}',
            'enablerepo': '{{ yum_enable_repo|default(omit) }}',
        }},
        {'service': {'name': 'mariadb', 'state': 'started'}}
    ]))

    # Call restore task
    for host in args.inventory:
        play = host_play(host, params={'role_action': 'restore'})
        play['roles'] = []
        for role in host["roles"]:
            # Only recover zuul data from the scheduler
            if role == 'zuul' and 'zuul-scheduler' not in host.get(
                    'params', {}).get('zuul_services', []):
                continue
            play['roles'].append({'role': "sf-%s" % role,
                                  'backup_src': '/var/lib/software-factory'
                                                '/backup/%s' % role})
        pb.append(play)

    pb.append(host_play('install-server',
                        tasks=notify_journald("recover ended")))


def setup(args, pb):
    action = {'role_action': 'setup'}
    # Setup install-server ssh keys
    pb.append(host_play('install-server', 'ssh', action))

    # Setup base role on all hosts
    for host in args.inventory:
        roles_action = {'role_action': 'setup', 'manage_etc_hosts': True}
        host_roles = ["postfix", "base"]
        if host.get("remote", False):
            # This host is running on isolated network
            host_roles = ["base"]
            roles_action['manage_etc_hosts'] = False
        pb.append(host_play(host, host_roles, roles_action))

    # Setup infra role firsts
    pre_roles = ("mysql",
                 "cauth", "keycloak",
                 "gateway", "hypervisor-openshift")
    for role in pre_roles:
        if role in args.glue["roles"]:
            pb.append(host_play(role, role, action))

    # Setup all components except infra roles
    for host in args.inventory:
        host_roles = [role for role in host["roles"] if role not in pre_roles]
        pb.append(host_play(host, host_roles, action))

    # Create config projects
    pb.append(host_play('install-server', 'repos', action))

    # During setup, we need to fetch config repos on every host, for example
    # to update connection list for zuul-merger and zuul-executor hosts.
    # During regular config-update, we don't update fetch the repos on those
    # host
    pb.append(host_play('all', 'repos', {'role_action': 'copy_config_repo'}))


def sync_config(args, pb):
    """Ensure config repo content is up-to-date."""
    pb.append(host_play('install-server', tasks=[
        dict(debug=dict(msg="Syncing config repo content")),
        dict(include_role=dict(name="sf-repos"),
             vars=dict(role_action="fetch_zuul_key")),
        dict(include_role=dict(name="sf-repos"),
             vars=dict(role_action="setup")),
    ]))


def config_update(args, pb, skip_sync=False):
    # The list of role to run update task
    roles_order = ["managesf", "gerrit", "pages", "gerritbot",
                   "gateway", "managesf", "mirror", "repoxplorer",
                   "zuul", "nodepool", "grafana", "hound", "dlrn",
                   "cgit", "cauth", "hypervisor-k1s"]
    # The extra list of host group to run fetch-config-repo
    roles_group = [
        "zuul-scheduler", "nodepool-launcher", "nodepool-builder",
    ]

    if not skip_sync:
        pb.append(host_play('install-server',
                            'repos', {'role_action': 'reset_config_repo'}))
        pb.append(host_play(
            "%s:!install-server" % ':'.join(roles_order + roles_group),
            'repos', {'role_action': 'copy_config_repo'}))

    # Update all components
    for host in args.inventory:
        host_roles = []
        if not skip_sync:
            # Skip host only running mergers or executors
            if (
                    host["roles"] == ["zuul"] and
                    set(host.get("params", {}).get(
                        "zuul_services", [])).issubset(
                            set(("zuul-merger", "zuul-executor")))):
                continue
        for role in roles_order:
            if role in host["roles"]:
                host_roles.append(role)
        pb.append(host_play(host, host_roles, {'role_action': 'update'}))


def nodepool_restart(args, pb):
    pb.append(host_play('nodepool-launcher', tasks=[{
        'name': 'Restart launcher',
        'service': {'name': 'nodepool-launcher',
                    'state': 'restarted'}}]))

    if 'nodepool-builder' in args.glue["roles"]:
        pb.append(host_play('nodepool-builder', tasks=[{
            'name': 'Restart builders',
            'service': {'name': 'nodepool-builder',
                        'state': 'restarted'}}]))


def zuul_restart(args, pb):
    dump_file = '/var/lib/software-factory/state/zuul-change-dump.sh'

    pb.append(host_play('zuul-executor', tasks=[{
        'name': 'Stop executors',
        'command': '/bin/zuul-executor stop'
    }]))

    pb.append(host_play('zuul-scheduler', tasks=[{
        'name': 'Dump changes',
        'command': ('/usr/libexec/software-factory/zuul-changes.py '
                    'dump --dump_file %s' % dump_file)
    }, {
        'name': 'Stop scheduler',
        'service': {'name': 'zuul-scheduler', 'state': 'stopped'}
    }]))

    pb.append(host_play('zuul-web', tasks=[{
        'name': 'Stop web',
        'service': {'name': 'zuul-web', 'state': 'stopped'}}]))

    if 'zuul-merger' in args.glue["roles"]:
        pb.append(host_play('zuul-merger', tasks=[{
            'name': 'Restart mergers',
            'service': {'name': 'zuul-merger',
                        'state': 'restarted'}}]))

    pb.append(host_play('zuul-executor', tasks=[{
        'name': 'Restart executors',
        'service': {'name': 'zuul-executor',
                    'state': 'restarted'}}]))

    pb.append(host_play('zuul-scheduler', tasks=[{
        'name': 'Restart scheduler',
        'service': {'name': 'zuul-scheduler',
                    'state': 'restarted'}}]))

    pb.append(host_play('zuul-web', tasks=[{
        'name': 'Restart web',
        'service': {'name': 'zuul-web', 'state': 'restarted'}
    }, {
        'name': 'Wait for scheduler reconfiguration',
        'uri': {
            'url': '{{ zuul_web_url }}/api/tenant/{{ tenant_name }}/pipelines',
            'return_content': 'yes',
            'status_code': '200'
        },
        'register': '_zuul_status',
        'until': (
            "'json' in _zuul_status and "
            "_zuul_status.json and "
            "'periodic' in _zuul_status.content"
        ),
        'retries': '900',
        'delay': '2'
    }]))

    pb.append(host_play('zuul-scheduler', tasks=[{
        'name': 'Reload zuul queues',
        'command': 'bash %s' % dump_file
    }]))


def notify_operator(args, pb):
    pb.append(host_play('install-server', 'repos', {'role_action': 'notify'}))


def tenant_update(args, pb):
    # Update tenant components managed on the main instance
    for host in args.inventory:
        host_roles = []
        for role in ["zuul"]:
            if role in host["roles"]:
                host_roles.append(role)
        pb.append(host_play(host, host_roles, {
            'role_action': 'update',
            'force_update': True}))


def postconf(args, pb):
    for host in args.inventory:
        pb.append(host_play(host, host["roles"], {'role_action': 'postconf'}))


def enable_action(args):
    pb = []

    if not (args.skip_setup and len(args.glue["inventory"]) == 1) and \
       not args.skip_populate_hosts:
        pb.append(host_play('install-server', 'ssh',
                            {'role_action': 'populate_hosts'}))
    playbook_name = "sfconfig"
    if args.update:
        playbook_name += "_update"
        update(args, pb)
    if not args.skip_install:
        install(args, pb)
    else:
        playbook_name += "_noinstall"
    if args.recover:
        playbook_name += "_recover"
        recover(args, pb)
    if not args.skip_setup:
        setup(args, pb)
        config_update(args, pb, skip_sync=True)
        postconf(args, pb)
        pb.append({
            'import_playbook': '%s/zuul_restart.yml' % args.ansible_root,
            'when': [
                'zuul_need_restart | default(False)',
                'not disable_zuul_autorestart | default(False) | bool'
            ]
        })
        pb.append({
            'import_playbook': '%s/nodepool_restart.yml' % args.ansible_root,
            'when': [
                'nodepool_need_restart | default(False)',
                'not disable_nodepool_autorestart | default(False) | bool'
            ]
        })
        pb.append({
            'import_playbook': '%s/sync_config.yml' % args.ansible_root,
            'when': ['not config_key_initialized | bool',
                     'not tenant_deployment']})
        # FIXME: zuul doesn't update on git push...
        pb.append({
            'import_playbook': '%s/zuul_restart.yml' % args.ansible_root,
            'when': ['not config_key_initialized | bool',
                     'not tenant_deployment',
                     'not disable_zuul_autorestart | default(False) | bool']})
    else:
        playbook_name += "_nosetup"

    # Store deployed version to be used by update playbook
    if not args.skip_setup:
        pb.append(host_play('install-server', tasks={
            'name': 'Write current version',
            'copy': {
                'dest': "/var/lib/software-factory/.version",
                'content': "{{ sf_version }}"
            }
        }))

    if not args.skip_setup and not args.skip_test:
        testinfra_tests = sfconfig.utils.list_testinfra()
        for host in args.glue["inventory"]:
            logfile = "/var/log/software-factory/testinfra.log"
            testinfra = ["testinfra", "-v", "--result-log=%s" % logfile]
            tests = []
            if "install-server" not in host["roles"]:
                testinfra.append("--connection=ssh")
                testinfra.append("--hosts=%s" % host["hostname"])
            for role in host["roles"]:
                if args.glue.get("tenant-deployment") and role == "gerrit":
                    continue
                if role in testinfra_tests:
                    tests.append(role)
            if host.get("remote") is not True:
                if 'influxdb' in host["roles"]:
                    if 'telegraf-statsd' in testinfra_tests:
                        tests.append('telegraf')
                        tests.append('telegraf-statsd')
            if tests:
                for test in tests:
                    testinfra.append(testinfra_tests[test])
                pb.append(host_play('install-server', tasks={
                    'name': 'Validate deployment with testinfra: %s' % tests,
                    'command': " ".join(testinfra),
                    'register': 'result',
                    'changed_when': 'False',
                    'until': 'result.rc == 0',
                    'retries': 60,
                    'delay': 1
                }))

    if not args.skip_setup:
        notify_operator(args, pb)

    return playbook_name, pb


def configure_ansible():
    ansible_cfg = "/var/lib/software-factory/ansible/ansible.cfg"
    ansiblecfg = configparser.ConfigParser()
    ansiblecfg.read("/usr/share/sf-config/ansible/ansible.cfg")
    ara_loc = "/usr/lib/python3.6/site-packages/ara/"
    if os.path.isdir(ara_loc):
        ansiblecfg.set("defaults", "callback_plugins",
                       "%s/plugins/callbacks" % ara_loc)
        ansiblecfg.set("defaults", "action_plugins",
                       "%s/plugins/actions" % ara_loc)
        ansiblecfg.set("defaults", "library",
                       "%s/plugins/modules" % ara_loc)
    ansiblecfg.write(open(ansible_cfg, "w"))
    os.environ["ANSIBLE_CONFIG"] = ansible_cfg
    os.environ["ARA_LOG_FILE"] = ""
    os.environ["ARA_DIR"] = "/var/lib/software-factory/ansible/ara/"


def install_ansible(args):
    if args.update:
        # Update ansible early on if possible
        sfconfig.utils.execute(["yum", "update", "-y", "ansible", "ara"])
    # TODO: figure out how to ensure we use the provided ansible and ara rpms
    if not os.path.isfile("/usr/bin/ansible-playbook"):
        sfconfig.utils.execute(["yum", "install", "-y", "ansible", "ara"])


def run(args):
    if args.disable or args.erase:
        playbook_name, playbook = disable_action(args)
    else:
        playbook_name, playbook = enable_action(args)

    playbook_path = "%s/%s.yml" % (args.ansible_root, playbook_name)
    write_playbook(playbook_path, playbook)
    os.environ["ANSIBLE_CONFIG"] = "%s/ansible/ansible.cfg" % args.share
    run_cmd = ["flock", "/var/lib/software-factory/state/ansible.lock",
               "ansible-playbook", playbook_path]

    if not args.skip_apply:
        os.chdir("/")
        install_ansible(args)
        configure_ansible()
        sfconfig.utils.execute(run_cmd)


def get_logs(args, pb):
    # Create /root/sf-logs directories
    tasks = [
        {'file': {'path': '/root/sf-logs', 'state': 'absent'},
         'name': 'Cleanup sf-logs directory'},
        {'file': {'mode': 448, 'path': '/root/sf-logs', 'state': 'directory'},
         'name': 'Create sf-logs directory'},
    ]
    for role in args.glue["roles"]:
        tasks.append({'name': 'Create %s log storage directory' % role,
                      'file': {'path': '/root/sf-logs/%s' % role,
                               'state': 'directory'}})
    pb.append(host_play('install-server', tasks=tasks))

    for host in args.inventory:
        play = host_play(host, params={'role_action': 'get_logs'})
        play['roles'] = []
        for role in ["base"] + host["roles"]:
            play['roles'].append({'role': "sf-%s" % role,
                                  'log_dest': '/root/sf-logs/%s' % role})
        pb.append(play)


def backup(args, pb):
    # Create local backup directory
    pb.append(host_play('install-server', tasks=[
        {'name': "Create backup directory",
         'file': {'path': '/var/lib/software-factory/backup',
                  'state': 'directory',
                  'mode': '0700'}},
        {'file': {'path': "/var/lib/software-factory/backup/{{ item }}",
                  'state': 'directory'},
         'loop': list(args.glue['roles'])}]))

    # Call backup task
    for host in args.inventory:
        play = host_play(host, params={'role_action': 'backup'})
        play['roles'] = []
        for role in host["roles"]:
            # Only backup zuul data from the scheduler
            if role == 'zuul' and 'zuul-scheduler' not in host.get(
                    'params', {}).get('zuul_services', []):
                continue
            play['roles'].append({'role': "sf-%s" % role,
                                  'backup_dest': '/var/lib/software-factory'
                                                 '/backup/%s' % role})
        pb.append(play)


def render_template(dest, template, data):
    if os.path.exists(dest):
        current = open(dest).read()
    else:
        current = ""
    loader = FileSystemLoader(os.path.dirname(template))
    env = Environment(trim_blocks=True, loader=loader)
    template = env.get_template(os.path.basename(template))
    new = template.render(data)
    if new[-1] != "\n":
        new += "\n"
    if new != current:
        with open(dest, "w") as out:
            out.write(new)
        print("[+] Wrote %s" % dest)


def generate(args):
    # Copy the original arch to add host params
    arch = copy.deepcopy(args.sfarch)

    # Adds playbooks to architecture
    for host in arch["inventory"]:
        # Host params are generic roles parameters
        host["params"] = {'host_public_url': host['public_url']}

        # This method handles roles such as zuul-merger that are in fact the
        # zuul role with the zuul_services argument set to "merger"
        def ensure_role_services(role_name, meta_names):
            # Ensure base role exists for metarole
            for meta_name in meta_names:
                # Add role services for meta role
                service_name = "%s-%s" % (role_name, meta_name)
                if service_name in host["roles"]:
                    host["params"].setdefault(
                        "%s_services" % role_name, []).append(service_name)
                    # Remove meta-name role
                    host["roles"] = list(filter(
                        lambda x: x != service_name, host["roles"]))
                    # Add base role to host
                    if role_name not in host["roles"]:
                        host["roles"].append(role_name)
                    if role_name not in args.glue["roles"]:
                        args.glue["roles"].setdefault(role_name, []).append(
                            host)

        ensure_role_services("nodepool", ["launcher", "builder"])
        ensure_role_services("zuul", ["scheduler", "merger", "executor",
                                      "web"])

        # if firehose role is in the arch, install publishers where needed
        if "firehose" in args.glue["roles"]:
            if "gerrit" in host["roles"]:
                host["roles"].append("germqtt")

        if "influxdb" in host["roles"]:
            # Add telegraf for statsd gateway
            host["roles"].append("telegraf")

    if 'hydrant' in args.glue["roles"] and \
       "firehose" not in args.glue["roles"]:
        raise RuntimeError("'hydrant' role needs 'firehose'")
    if 'hydrant' in args.glue["roles"] and \
       'elasticsearch' not in args.glue["roles"]:
        raise RuntimeError("'hydrant' role needs 'elasticsearch'")

    templates = "%s/templates" % args.share

    # Generate inventory
    arch["roles"] = args.glue["roles"]
    arch["hosts_file"] = args.glue["hosts_file"]
    render_template("%s/hosts" % args.ansible_root,
                    "%s/inventory.j2" % templates,
                    arch)

    # Generate playbooks
    args.inventory = arch["inventory"]
    for playbook_name, generator in (
            ("zuul_restart", zuul_restart),
            ("nodepool_restart", nodepool_restart),
            ("sync_config", sync_config),
            ("sf_configrepo_update", config_update),
            ("sf_tenant_update", tenant_update),
            ("get_logs", get_logs),
            ("sf_backup", backup),
            ("sf_erase", erase)):
        playbook_path = "%s/%s.yml" % (args.ansible_root, playbook_name)
        playbook = []
        generator(args, playbook)
        write_playbook(playbook_path, playbook)

    # Generate /etc/hosts file
    # including network static_hostname defined in sfconfig.yaml
    host_arch = copy.copy(arch)
    host_arch["network"] = args.sfconfig["network"]
    render_template("/etc/hosts",
                    "%s/etc-hosts.j2" % templates,
                    host_arch)
