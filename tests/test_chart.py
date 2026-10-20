"""Render the chart with synthetic identities and inspect its isolation contract.

Run with: python3 -m unittest discover -s tests -p test_chart.py
Set HELM_BIN when Helm is not on PATH. PyYAML is required.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from gateway import validate_config


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'charts' / 'telegram-bot-suite'
HELM = os.environ.get('HELM_BIN') or shutil.which('helm')


def worker(identifier, uid, topic):
    return {
        'id': identifier, 'uid': uid,
        'command': ['python3', '/app/service.py'],
        'route': {'chat_id': -1000000000123, 'topic_id': topic,
                  'receives_updates': False, 'allowed_user_ids': [],
                  'methods': ['getMe', 'sendMessage']},
        'env': {'EXAMPLE_STATE_PATH': '/data/state.json'},
        'config': {'example.json': '{"synthetic":true}'},
        'persistence': {'size': '128Mi', 'storageClassName': 'test-storage'},
    }


def synthetic_values():
    first = worker('acorn', 11001, 11)
    first['persistence'] = {'existingClaim': 'old-worker-state'}
    first['route'].update(receives_updates=True, allowed_user_ids=[12345678901, 123456789012345])
    first['ports'] = [{'name': 'acorn-health', 'containerPort': 8080}]
    first['startupProbe'] = {'httpGet': {'path': '/livez', 'port': 'acorn-health'},
                             'periodSeconds': 5, 'failureThreshold': 24}
    first['livenessProbe'] = {'httpGet': {'path': '/livez', 'port': 8080}, 'periodSeconds': 10}
    first['readinessProbe'] = {'httpGet': {'path': '/readyz', 'port': 'acorn-health'}}
    second = worker('greenwich', 11002, 22)
    second['command'] = ['python3', '/app/greenwich/runner.py']
    second['image'] = 'registry.example/greenwich@sha256:' + 'b' * 64
    second['config'] = {}
    second['secretMounts'] = [
        {'secretName': 'example-property', 'mountPath': '/etc/property',
         'items': [{'key': 'property.json', 'path': 'property.json'}]},
        {'secretName': 'example-credentials', 'mountPath': '/etc/credentials',
         'items': [{'key': 'credential', 'path': 'credential'}]},
    ]
    second['resources'] = {'requests': {'cpu': '20m', 'memory': '48Mi'},
                           'limits': {'cpu': '500m', 'memory': '256Mi'}}
    for name in ('startupProbe', 'livenessProbe', 'readinessProbe'):
        second[name] = {'exec': {'command': ['python3', '/app/greenwich/runner.py', 'health']},
                        'periodSeconds': 10}
    return {'replicas': 0, 'fullnameOverride': 'synthetic-suite',
            'image': 'registry.example/suite@sha256:' + 'a' * 64,
            'gateway': {'tokenSecret': {'name': 'test-only-token', 'key': 'token'},
                        'persistence': {'size': '256Mi', 'storageClassName': ''}},
            'legacyAcorn': {'workerId': 'acorn'}, 'workers': [first, second]}


@unittest.skipUnless(HELM, 'Helm is required; set HELM_BIN')
class ChartTests(unittest.TestCase):
    def render(self, values=None, *, failure=False, skip_schema=False):
        with tempfile.TemporaryDirectory() as directory:
            command = [HELM, 'template', 'test', str(CHART), '--namespace', 'test-only']
            if values is not None:
                path = Path(directory) / 'values.json'
                path.write_text(json.dumps(values))
                command += ['--values', str(path)]
            if skip_schema:
                command.append('--skip-schema-validation')
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        if failure:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def objects(self, values=None):
        docs = self.render(values)
        registry = next(d for d in docs if d['kind'] == 'ConfigMap' and 'registry.json' in d['data'])
        configuration = json.loads(registry['data']['registry.json'])
        if configuration['workers']:
            validate_config(configuration)
        deployment = next(d for d in docs if d['kind'] == 'Deployment')
        return docs, deployment, deployment['spec']['template']['spec']

    @staticmethod
    def env(container):
        return {e['name']: e['value'] for e in container['env']}

    @staticmethod
    def mounts(container):
        return {m['mountPath']: m for m in container['volumeMounts']}

    def test_default_lints_and_renders_suspended_gateway_without_app_defaults(self):
        result = subprocess.run([HELM, 'lint', str(CHART), '--strict'], text=True,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        docs, deployment, pod = self.objects()
        self.assertEqual(deployment['spec']['replicas'], 0)
        self.assertEqual([c['name'] for c in pod['containers']], ['gateway'])
        self.assertNotIn('initContainers', pod)
        registry = next(d for d in docs if d['kind'] == 'ConfigMap')['data']['registry.json']
        self.assertEqual(json.loads(registry)['workers'], [])
        defaults = yaml.safe_load((CHART / 'values.yaml').read_text())
        self.assertEqual(defaults['workers'], [])
        self.assertEqual(defaults['legacyAcorn']['workerId'], '')
        self.assertEqual(yaml.safe_load((CHART / 'Chart.yaml').read_text())['version'], '0.1.0')
        self.render({'replicas': 1}, failure=True)

    def test_registry_env_and_config_are_generated_from_same_worker_list(self):
        values = synthetic_values()
        docs, _, pod = self.objects(values)
        configmaps = {d['metadata']['name']: d for d in docs if d['kind'] == 'ConfigMap'}
        registry = json.loads(configmaps['synthetic-suite-registry']['data']['registry.json'])
        self.assertEqual(registry['state_path'], '/gateway/private/state.sqlite3')
        self.assertEqual(len(registry['workers']), 2)
        containers = {c['name']: c for c in pod['containers']}
        self.assertEqual(set(containers), {'acorn', 'greenwich', 'gateway'})
        for w, route in zip(values['workers'], registry['workers']):
            self.assertEqual(route, dict(w['route'], id=w['id'], uid=w['uid'],
                                         socket_path=f"/run/workers/{w['id']}/private/api.sock"))
            c = containers[w['id']]
            env = self.env(c)
            self.assertEqual(env['TELEGRAM_GATEWAY_SOCKET'], '/run/telegram/private/api.sock')
            self.assertEqual(env['TELEGRAM_CHAT_ID'], str(route['chat_id']))
            self.assertEqual(env['TELEGRAM_TOPIC_ID'], str(route['topic_id']))
            self.assertEqual(env['TELEGRAM_ALLOWED_USER_IDS'], ','.join(map(str, route['allowed_user_ids'])))
            self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
            self.assertEqual(env['PYTHONUNBUFFERED'], '1')
            self.assertEqual(len(c['env']), len(env))
            self.assertNotIn('envFrom', c)
            self.assertTrue(all(set(e) == {'name', 'value'} and isinstance(e['value'], str) for e in c['env']))
            self.assertEqual(configmaps[f"synthetic-suite-{w['id']}-config"]['data'], w['config'])
            self.assertEqual(c['image'], w.get('image', values['image']))
        self.assertEqual(containers['greenwich']['resources'], values['workers'][1]['resources'])

    def test_third_worker_needs_no_template_changes_and_scales_to_one(self):
        values = synthetic_values()
        values['workers'].append(worker('third', 11004, 33))
        values['replicas'] = 1
        docs, deployment, pod = self.objects(values)
        self.assertEqual(deployment['spec']['replicas'], 1)
        self.assertEqual(deployment['spec']['strategy'], {'type': 'Recreate'})
        self.assertEqual([c['name'] for c in pod['containers']], ['acorn', 'greenwich', 'third', 'gateway'])
        registry = next(d for d in docs if d.get('data', {}).get('registry.json'))
        self.assertEqual(len(json.loads(registry['data']['registry.json'])['workers']), 3)
        gateway = pod['containers'][-1]
        self.assertIn('/run/workers/third', self.mounts(gateway))
        self.assertNotIn('/data', self.mounts(gateway))

    def test_uid_mount_and_token_isolation_and_hardening(self):
        _, deployment, pod = self.objects(synthetic_values())
        self.assertEqual(deployment['spec']['strategy']['type'], 'Recreate')
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod['shareProcessNamespace'])
        for key in ('hostNetwork', 'hostPID', 'hostIPC', 'serviceAccountName'):
            self.assertNotIn(key, pod)
        self.assertEqual(pod['securityContext']['fsGroup'], 10001)
        self.assertEqual(pod['terminationGracePeriodSeconds'], 75)
        volumes = {v['name']: v for v in pod['volumes']}
        self.assertEqual(len(volumes), len(pod['volumes']))
        token = next(v['name'] for v in pod['volumes'] if v.get('secret', {}).get('secretName') == 'test-only-token')
        for c in pod['containers'] + pod['initContainers']:
            sc = c['securityContext']
            self.assertTrue(sc['runAsNonRoot'])
            self.assertGreater(sc['runAsUser'], 0)
            self.assertEqual(sc['runAsGroup'], 10001)
            self.assertTrue(sc['readOnlyRootFilesystem'])
            self.assertFalse(sc['allowPrivilegeEscalation'])
            self.assertEqual(sc['capabilities'], {'drop': ['ALL']})
            self.assertEqual(sc['seccompProfile'], {'type': 'RuntimeDefault'})
            self.assertNotIn('privileged', sc)
            mounted = {m['name'] for m in c['volumeMounts']}
            self.assertTrue(mounted <= volumes.keys())
            if c['name'] != 'gateway':
                self.assertNotIn(token, mounted)
                self.assertFalse(any('TOKEN' in key for key in self.env(c)))
        gateway = pod['containers'][-1]
        self.assertEqual(gateway['securityContext']['runAsUser'], 10003)
        self.assertIn(token, {m['name'] for m in gateway['volumeMounts']})
        self.assertEqual(self.env(gateway)['TELEGRAM_BOT_TOKEN_FILE'], '/var/run/telegram/token')
        self.assertTrue(self.mounts(gateway)['/var/run/telegram']['readOnly'])
        workers = pod['containers'][:-1]
        worker_volumes = []
        for c in workers:
            mounts = self.mounts(c)
            self.assertTrue({'/data', '/etc/worker', '/run/telegram', '/tmp'} <= mounts.keys())
            for path in ('/etc/worker', '/run/telegram'):
                self.assertTrue(mounts[path]['readOnly'])
            self.assertNotIn('/gateway', mounts)
            self.assertTrue(all(not p.startswith('/run/workers/') for p in mounts))
            worker_volumes.append({m['name'] for m in c['volumeMounts']})
            self.assertEqual(volumes[mounts['/run/telegram']['name']]['emptyDir'], {'medium': 'Memory', 'sizeLimit': '1Mi'})
            self.assertEqual(volumes[mounts['/tmp']['name']]['emptyDir'], {'sizeLimit': '16Mi'})
        self.assertTrue(worker_volumes[0].isdisjoint(worker_volumes[1]))
        self.assertEqual(volumes['worker-1-secret-0']['secret']['defaultMode'], 0o440)
        for path in ('/etc/property', '/etc/credentials'):
            self.assertTrue(self.mounts(workers[1])[path]['readOnly'])
        for v in pod['volumes']:
            self.assertNotIn('hostPath', v)

    def test_persistence_retains_new_claims_and_does_not_adopt_existing_claims(self):
        values = synthetic_values()
        docs, _, pod = self.objects(values)
        claims = {d['metadata']['name']: d for d in docs if d['kind'] == 'PersistentVolumeClaim'}
        self.assertEqual(set(claims), {'synthetic-suite-gateway-state', 'synthetic-suite-greenwich-state'})
        for claim in claims.values():
            self.assertEqual(claim['metadata']['annotations'], {
                'helm.sh/resource-policy': 'keep', 'kustomize.toolkit.fluxcd.io/prune': 'disabled'})
            self.assertEqual(claim['spec']['accessModes'], ['ReadWriteOnce'])
        self.assertEqual(claims['synthetic-suite-gateway-state']['spec']['storageClassName'], '')
        self.assertEqual(claims['synthetic-suite-gateway-state']['spec']['resources']['requests']['storage'], '256Mi')
        self.assertEqual(claims['synthetic-suite-greenwich-state']['spec']['storageClassName'], 'test-storage')
        self.assertEqual(pod['volumes'][4]['persistentVolumeClaim']['claimName'], 'old-worker-state')
        values['gateway']['persistence'] = {'existingClaim': 'old-gateway-state'}
        values['workers'][1]['persistence'] = {'existingClaim': 'other-worker-state'}
        docs, _, pod = self.objects(values)
        self.assertFalse(any(d['kind'] == 'PersistentVolumeClaim' for d in docs))
        mounted_claims = {v['persistentVolumeClaim']['claimName'] for v in pod['volumes'] if 'persistentVolumeClaim' in v}
        self.assertEqual(mounted_claims, {'old-gateway-state', 'old-worker-state', 'other-worker-state'})
        values['workers'][0]['persistence']['existingClaim'] = 'acorn-care-bot-state'
        docs, _, pod = self.objects(values)
        self.assertFalse(any(d['kind'] == 'PersistentVolumeClaim' for d in docs))
        self.assertIn('acorn-care-bot-state', {v['persistentVolumeClaim']['claimName'] for v in pod['volumes'] if 'persistentVolumeClaim' in v})

    def test_probes_and_migration_are_tied_to_selected_worker(self):
        values = synthetic_values()
        _, _, pod = self.objects(values)
        containers = {c['name']: c for c in pod['containers']}
        for w in values['workers']:
            for probe in ('startupProbe', 'livenessProbe', 'readinessProbe'):
                self.assertEqual(containers[w['id']][probe], w[probe])
        self.assertEqual(self.env(containers['gateway'])['GATEWAY_HTTP_PORT'], '8081')
        self.assertEqual(containers['gateway']['readinessProbe']['httpGet'], {'path': '/readyz', 'port': 'gateway-health'})
        init = pod['initContainers'][0]
        self.assertEqual(init['command'], ['python3', '/app/gateway_seed.py'])
        self.assertEqual(init['securityContext']['runAsUser'], 11001)
        self.assertEqual(self.mounts(init)['/legacy'], {'name': 'worker-0-state', 'mountPath': '/legacy', 'readOnly': True})
        self.assertEqual(set(self.mounts(init)), {'/legacy', '/gateway'})
        self.assertEqual(self.env(init)['GATEWAY_LEGACY_PATH'], self.env(containers['gateway'])['GATEWAY_LEGACY_PATH'])
        self.assertEqual(self.env(init)['TELEGRAM_CHAT_ID'], str(values['workers'][0]['route']['chat_id']))
        self.assertEqual(self.env(init)['TELEGRAM_TOPIC_ID'], '11')
        values['legacyAcorn']['workerId'] = 'greenwich'
        self.render(values, failure=True)
        values['legacyAcorn']['workerId'] = ''
        self.assertNotIn('initContainers', self.objects(values)[2])

    def test_config_and_registry_checksums_change_deterministically(self):
        values = synthetic_values()
        def annotations(v):
            return self.objects(v)[1]['spec']['template']['metadata']['annotations']
        before = annotations(values)
        self.assertEqual(before, annotations(json.loads(json.dumps(values))))
        self.assertTrue(all(len(value) == 64 for value in before.values()))
        values['workers'][0]['config']['example.json'] = '{"synthetic":false}'
        after_config = annotations(values)
        self.assertNotEqual(before['checksum/configuration'], after_config['checksum/configuration'])
        self.assertEqual(before['checksum/registry'], after_config['checksum/registry'])
        values['workers'][0]['route']['topic_id'] = 99
        after_route = annotations(values)
        self.assertNotEqual(after_config['checksum/registry'], after_route['checksum/registry'])
        self.assertNotEqual(after_config['checksum/configuration'], after_route['checksum/configuration'])

    def test_network_policy_preserves_dns_public_https_only(self):
        docs = self.render(synthetic_values())
        policy = next(d for d in docs if d['kind'] == 'NetworkPolicy')['spec']
        self.assertEqual(policy['ingress'], [])
        self.assertEqual(policy['policyTypes'], ['Ingress', 'Egress'])
        dns, https = policy['egress']
        self.assertEqual(dns, {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                                     'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                               'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]})
        self.assertEqual(https['ports'], [{'protocol': 'TCP', 'port': 443}])
        self.assertEqual(https['to'], [
            {'ipBlock': {'cidr': '0.0.0.0/0', 'except': [
                '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
                '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15',
                '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4']}},
            {'ipBlock': {'cidr': '::/0', 'except': [
                '::/96', '::ffff:0:0/96', '64:ff9b::/96', '64:ff9b:1::/48', '100::/64',
                '2001::/32', '2001:db8::/32', '2002::/16', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8']}}])
        self.assertEqual(policy['podSelector']['matchLabels'],
                         next(d for d in docs if d['kind'] == 'Deployment')['spec']['selector']['matchLabels'])
        self.assertFalse(any(d['kind'] in ('Service', 'Ingress', 'Secret', 'ServiceAccount') for d in docs))

    def test_duplicate_and_invalid_identities_routes_claims_and_ports_fail(self):
        cases = [
            ('root uid', lambda v: v['workers'][0].update(uid=0)),
            ('negative uid', lambda v: v['workers'][0].update(uid=-1)),
            ('fractional uid', lambda v: v['workers'][0].update(uid=11001.5)),
            ('gateway uid', lambda v: v['workers'][0].update(uid=10003)),
            ('duplicate uid', lambda v: v['workers'][1].update(uid=11001)),
            ('duplicate id', lambda v: v['workers'][1].update(id='acorn')),
            ('gateway id', lambda v: v['workers'][0].update(id='gateway')),
            ('duplicate route', lambda v: v['workers'][1]['route'].update(topic_id=11)),
            ('zero chat', lambda v: v['workers'][0]['route'].update(chat_id=0)),
            ('zero topic', lambda v: v['workers'][0]['route'].update(topic_id=0)),
            ('users required', lambda v: v['workers'][0]['route'].update(allowed_user_ids=[])),
            ('duplicate claim', lambda v: v['workers'][1].update(persistence={'existingClaim': 'old-worker-state'})),
            ('gateway claim', lambda v: v['workers'][0].update(persistence={'existingClaim': 'synthetic-suite-gateway-state'})),
            ('gateway health', lambda v: v['workers'][0]['ports'][0].update(containerPort=8081)),
            ('duplicate health', lambda v: v['workers'][1].update(ports=[{'name': 'other-health', 'containerPort': 8080}])),
            ('duplicate port name', lambda v: v['workers'][1].update(ports=[{'name': 'acorn-health', 'containerPort': 8082}])),
            ('bad probe', lambda v: v['workers'][0]['readinessProbe']['httpGet'].update(port='missing-health')),
            ('bad migration', lambda v: v['legacyAcorn'].update(workerId='missing')),
            ('unsupported migration', lambda v: v['legacyAcorn'].update(workerId='greenwich')),
            ('replicas', lambda v: v.update(replicas=2)),
        ]
        for label, mutate in cases:
            for skip_schema in (False, True):
                with self.subTest(case=label, skip_schema=skip_schema):
                    values = synthetic_values()
                    mutate(values)
                    self.render(values, failure=True, skip_schema=skip_schema)

    def test_unsafe_worker_env_and_secret_mounts_fail(self):
        for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN_FILE', 'OTHER_TOKEN',
                     'telegram_token', 'TELEGRAM_GATEWAY_SOCKET', 'TELEGRAM_CHAT_ID',
                     'TELEGRAM_TOPIC_ID', 'TELEGRAM_ALLOWED_USER_IDS',
                     'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED'):
            with self.subTest(env=name):
                values = synthetic_values()
                values['workers'][0]['env'][name] = 'forbidden'
                self.render(values, failure=True, skip_schema=True)
        for path in ('/', '/data', '/data/nested', '/etc', '/etc/worker', '/etc/worker/nested',
                     '/tmp', '/run', '/run/telegram', '/var/run/other', '/app', '/proc', '/sys',
                     '/dev', '/gateway', '/legacy', 'relative', '/etc/property/../unsafe',
                     '/etc//unsafe', '/etc/property/', '/etc/property/nested', '/etc/property'):
            with self.subTest(path=path):
                values = synthetic_values()
                values['workers'][1]['secretMounts'][1]['mountPath'] = path
                self.render(values, failure=True, skip_schema=True)
        values = synthetic_values()
        values['workers'][1]['secretMounts'][0]['secretName'] = 'test-only-token'
        self.assertIn('gateway token secret', self.render(values, failure=True, skip_schema=True))

    def test_schema_rejects_unstructured_overrides_and_invalid_values(self):
        cases = [
            lambda v: v['workers'][0]['env'].update(SECRET={'valueFrom': {'secretKeyRef': {}}}),
            lambda v: v['workers'][0]['env'].update(NUMBER=123),
            lambda v: v['workers'][0].update(envFrom=[{'secretRef': {'name': 'test-only-token'}}]),
            lambda v: v['workers'][0].update(volumes=[{'hostPath': {'path': '/'}}]),
            lambda v: v['workers'][0].update(securityContext={'privileged': True}),
            lambda v: v['workers'][0].update(uid='11001'),
            lambda v: v['workers'][0]['route'].update(methods=['anything']),
            lambda v: v['workers'][0]['route'].update(methods=['getMe', 'getMe']),
            lambda v: v['workers'][0]['route'].update(allowed_user_ids=[123, 123]),
            lambda v: v['workers'][0]['route'].update(chatId=-123),
            lambda v: v['workers'][0]['route'].update(chat_id=-9007199254740993),
            lambda v: v['workers'][0]['route'].update(topic_id=9007199254740993),
            lambda v: v['workers'][0]['route'].update(allowed_user_ids=[9007199254740993]),
            lambda v: v['workers'][0]['config'].update(bad=123),
            lambda v: v['workers'][0]['config'].update({'../unsafe': 'x'}),
            lambda v: v['workers'][1]['secretMounts'][0]['items'][0].update(path='../token'),
            lambda v: v['workers'][0]['ports'][0].update(hostPort=80),
            lambda v: v['workers'][0]['readinessProbe']['httpGet'].update(host='other.example'),
            lambda v: v['workers'][0]['readinessProbe'].update(exec={'command': ['true']}),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                values = synthetic_values()
                mutate(values)
                self.render(values, failure=True)


if __name__ == '__main__':
    unittest.main()
