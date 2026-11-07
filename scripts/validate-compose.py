#!/usr/bin/env python3
"""Validate the release, build override, and optional test stack without starting them."""
import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
SERVICES={'app','browser','proxy','gateway','ingress','page-reader'}
INFERENCE={'KIWM_OMLX_URL','KIWM_OMLX_API_KEY','KIWM_MODEL','KIWM_AGENT_MODEL','KIWM_ANALYSIS_MODEL','KIWM_VISION_MODEL'}
COMMUNICATION={'KIWM_TELEGRAM_BOT_TOKEN','KIWM_TELEGRAM_CHAT_ID','KIWM_TELEGRAM_MODE',
    'KIWM_SMTP_ENABLED','KIWM_SMTP_HOST','KIWM_SMTP_PORT','KIWM_SMTP_TLS','KIWM_SMTP_USER',
    'KIWM_SMTP_PASSWORD','KIWM_SMTP_FROM','KIWM_SMTP_TO'}

def require(condition,message):
    if not condition:raise ValueError(message)

def portainer_source(source):
    return source

def render(paths,env_file=None,extra_env=None):
    args=['docker','compose','--project-directory',str(ROOT)]
    if env_file:args+=['--env-file',str(Path(env_file).resolve())]
    for path in paths:args+=['-f',str(ROOT/path)]
    args+=['config','--format','json']
    r=subprocess.run(args,capture_output=True,text=True,env={**os.environ, **(extra_env or {})})
    if r.returncode:raise ValueError('Compose rejected configuration; inspect locally without publishing environment values.')
    return json.loads(r.stdout)

def check_runtime(model,image_only=False):
    services=model['services'];names=set(services)
    require(names in (SERVICES,SERVICES|{'mailpit','fixture'}),'Expected six release or eight test services')
    tests='fixture' in services
    require(model['networks']['monitor'].get('internal') is True,'Monitor network must be internal')
    subnet=ipaddress.ip_network(model['networks']['monitor']['ipam']['config'][0]['subnet'])
    require(subnet.version==4 and any(subnet.subnet_of(ipaddress.ip_network(x)) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')),'Private IPv4 subnet required')
    require(services['app']['environment']['BROWSER_CDP_URL']=='http://browser:9223','Use Docker DNS for the browser')
    require(services['gateway']['environment']['MONITOR_SUBNET']==str(subnet),'Gateway boundary must match network')
    for name,s in services.items():
        require(s.get('user')=={'proxy':'proxy','ingress':'101:101'}.get(name,'1000:1000'),name+': unexpected user')
        require(s.get('read_only') and s.get('cap_drop')==['ALL'] and 'no-new-privileges:true' in s.get('security_opt',[]),name+': missing security boundary')
        require(not s.get('privileged') and not s.get('cap_add') and not s.get('devices'),name+': elevated access')
        require(s.get('restart')=='unless-stopped',name+': missing restart policy')
        require(not s.get('healthcheck',{}).get('disable'),name+': health probe disabled')
        require(not image_only or 'build' not in s,name+': release must use ready images')
        require(not s.get('ports') or name=='ingress',name+': browser/admin ports must remain private')
        expected={'monitor','outbound'} if name in ('proxy','gateway','ingress') else set() if name=='page-reader' else {'monitor'}
        require(set(s.get('networks',{}))==expected,name+': unexpected network access')
        for dependency in s.get('depends_on',{}).values():require(dependency['condition']=='service_healthy',name+': dependency must wait for readiness')
        for v in s.get('volumes',[]):
            require(v['type'] in ('bind','volume'),name+': unsupported storage')
            require('docker.sock' not in v.get('source','') and 'docker.sock' not in v['target'],name+': Docker socket forbidden')
        require(not INFERENCE.intersection(s.get('environment',{})) or name in ('app','gateway'),name+': inference secrets must stay in app/gateway')
        require(not COMMUNICATION.intersection(s.get('environment',{})) or name in ('app','gateway'),name+': communication secrets must stay in app/gateway')
    reader = services['page-reader']
    socket_dir = '/run/kiwm-page-reader'
    require(reader.get('network_mode') == 'none' and not reader.get('environment'), 'Page reader must be offline and secret-free')
    require([(v['type'],v['source'],v['target']) for v in reader['volumes']]
            == [('volume','page-reader-socket',socket_dir)], 'Page reader may only mount its Unix socket volume')
    require(any(v['source']=='page-reader-socket' and v['target']==socket_dir and v.get('read_only')
                for v in services['app']['volumes']), 'App must connect over the read-only socket volume')
    admin={'KIWM_ADMIN_USERNAME','KIWM_ADMIN_PASSWORD','KIWM_SESSION_HOURS'}
    require(admin<=set(services['app']['environment']),'Missing administrator ENV')
    require(all(not admin.intersection(s.get('environment',{})) for n,s in services.items() if n!='app'),'Admin credentials must stay in app')
    require(all(INFERENCE<=set(services[n]['environment']) for n in ('app','gateway')),'Missing inference overrides')
    require(all(services['app']['environment'][k]==services['gateway']['environment'][k] for k in INFERENCE),'Inference overrides disagree')
    require(all(COMMUNICATION<=set(services[n]['environment']) for n in ('app','gateway')),'Missing communication overrides')
    require(all(services['app']['environment'][k]==services['gateway']['environment'][k] for k in COMMUNICATION),'Communication overrides disagree')
    require(all(v.get('read_only') is True for v in services['gateway']['volumes']),'Gateway config must be read-only')
    lan_url=services['app']['environment'].get('KIWM_LAN_URL')
    ports=services['ingress']['ports'];targets={8080,8081,8082} if tests else {8080,8081}
    if lan_url:
        from urllib.parse import urlsplit
        parsed=urlsplit(lan_url)
        lan_ip=ipaddress.IPv4Address(parsed.hostname)
        lan_env=services['ingress']['environment']
        network=ipaddress.IPv4Network(lan_env['KIWM_LAN_SUBNET'])
        require(parsed.scheme=='http' and parsed.port and 1024<=parsed.port<=65535 and not parsed.path and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,'Explicit HTTP LAN origin required')
        require(any(network.subnet_of(ipaddress.ip_network(x)) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')) and lan_ip in network,'LAN subnet must be private and contain the endpoint')
        require(lan_env['KIWM_LAN_IP']==str(lan_ip) and int(lan_env['KIWM_LAN_PORT'])==parsed.port,'LAN settings disagree')
        lan_ports=[p for p in ports if p['target']==8083]
        require(len(lan_ports)==1 and lan_ports[0].get('host_ip')==str(lan_ip) and int(lan_ports[0]['published'])==parsed.port,'LAN port must bind exactly the configured private address')
        require(all(not s.get('environment',{}).get('KIWM_LAN_URL') for n,s in services.items() if n!='app'),'LAN origin belongs only in app')
        targets.add(8083)
    require(len(ports)==len(targets) and {x['target'] for x in ports}==targets,'Unexpected published endpoints')
    require(all(x.get('host_ip')=='127.0.0.1' for x in ports if x['target']!=8083),'Loopback-only ports required')
    require(len({(x.get('host_ip'),x['published']) for x in ports})==len(ports),'Distinct ports required')
    if tests:
        fixture=services['fixture']['networks']['monitor']['ipv4_address']
        require(ipaddress.ip_address(fixture) in subnet,'Fixture outside monitor subnet')
        require(services['proxy']['environment']['FIXTURE_IP']==fixture,'Test exception must match fixture')
    else:
        require(not services['proxy'].get('environment',{}).get('FIXTURE_IP'),'Production must not allow a private fixture target')
        require(not services['gateway']['environment'].get('KIWM_TEST_SMTP_HOST'),'Test mail service must be optional')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--write-portainer',action='store_true');p.add_argument('--source-only',action='store_true');p.add_argument('--env-file')
    args=p.parse_args();canonical=(ROOT/'compose.yaml').read_text(encoding='utf-8')
    if args.write_portainer:(ROOT/'portainer-stack.yaml').write_text(canonical,encoding='utf-8',newline='\n')
    require((ROOT/'portainer-stack.yaml').read_text(encoding='utf-8')==canonical,'Release Compose and Portainer source differ')
    if args.source_only:print('PASS: identical release Compose and Portainer source');return
    core=render(['compose.yaml'],args.env_file);check_runtime(core,image_only=True)
    require(core==render(['portainer-stack.yaml'],args.env_file),'Release models differ')
    build=render(['compose.yaml','compose.build.yaml'],args.env_file)
    require({n for n,s in build['services'].items() if 'build' in s}==SERVICES-{'gateway'},'Expected five image builds')
    for s in build['services'].values():s.pop('build',None)
    require(core==build,'Build override changes runtime')
    check_runtime(render(['compose.yaml','compose.test.yaml'],args.env_file),image_only=True)
    lan_env={'KIWM_LAN_IP':'192.168.50.10','KIWM_LAN_PORT':'18443','KIWM_LAN_SUBNET':'192.168.50.0/24'}
    lan=render(['compose.yaml','compose.lan.yaml'],args.env_file,lan_env)
    check_runtime(lan,image_only=True)
    require(lan==render(['portainer-lan-stack.yaml'],args.env_file,lan_env),'LAN Portainer stack differs from Compose overlay')
    print('PASS: six release services, offline page reader, optional LAN HTTP and test stacks, five image builds')

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError) as e:
        print('FAIL: '+str(e),file=sys.stderr);raise SystemExit(1)
