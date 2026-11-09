"""Check the bot-owned home manifests and render the home chart without a cluster.

Run with python3 -m unittest discover -s tests -p test_home_deployment.py.
Set HELM_BIN or KUBECTL_BIN when the tools are not on PATH.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from configuration import load_care_config, validate_care_config
from gateway import validate_config


ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / 'deploy' / 'standalone'
SUITE = ROOT / 'deploy' / 'suite'
CHART = ROOT / 'charts' / 'telegram-bot-suite'
HOME = CHART / 'values-home.yaml'
NAMESPACE = 'acorn-care-bot'
PLACEHOLDER = 'ghcr.io/lionzxyorg/acorn-care-bot:deployment-must-pin'


def binary(name):
    configured = os.environ.get(name.upper() + '_BIN')
    if configured:
        return configured
    found = shutil.which(name)
    if found:
        return found
    for parent in ROOT.parents:
        candidate = parent / '.tools' / 'bin' / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


HELM = binary('helm')
KUBECTL = binary('kubectl')


def read(path):
    return yaml.safe_load(path.read_text())


def documents(text):
    return [obj for obj in yaml.safe_load_all(text) if obj]


def properties():
    return dict(line.split('=', 1) for line in
                (STANDALONE / 'runtime.properties').read_text().splitlines()
                if line.strip() and not line.lstrip().startswith('#'))


def run(command):
    # A failed render or timeout must fail the test, not silently skip it.
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                            timeout=60, check=False)
    if result.returncode:
        raise AssertionError(f'{command!r} failed ({result.returncode}):\n'
                             f'{result.stdout}\n{result.stderr}')
    return result.stdout


def deployment(objects):
    matches = [obj for obj in objects if obj['kind'] == 'Deployment']
    if len(matches) != 1:
        raise AssertionError(f'Expected one Deployment, found {len(matches)}')
    return matches[0]


def env(container):
    return {entry['name']: entry['value'] for entry in container['env']}


def mounts(container):
    return {entry['mountPath']: entry for entry in container['volumeMounts']}


class StandaloneHomeTests(unittest.TestCase):
    def setUp(self):
        self.deployment = read(STANDALONE / 'deployment.yaml')
        self.spec = self.deployment['spec']
        self.pod = self.spec['template']['spec']
        self.assertEqual(len(self.pod['containers']), 1)
        self.container = self.pod['containers'][0]

    def test_single_writer_and_fail_closed_image(self):
        self.assertEqual(self.spec['replicas'], 1)
        self.assertEqual(self.spec['strategy'], {'type': 'Recreate'})
        self.assertEqual(self.container['image'], PLACEHOLDER)
        self.assertNotIn('imagePullSecrets', self.pod)
        self.assertEqual(self.deployment['metadata']['name'], NAMESPACE)
        self.assertEqual(self.spec['selector']['matchLabels'], {'app': NAMESPACE})
        self.assertEqual(self.spec['template']['metadata']['labels'], {'app': NAMESPACE})
        kustomization = read(STANDALONE / 'kustomization.yaml')
        self.assertEqual(kustomization['namespace'], NAMESPACE)
        self.assertEqual(kustomization['resources'], ['deployment.yaml', 'storage.yaml'])
        self.assertNotIn('images', kustomization)

    def test_token_is_a_required_read_only_secret_reference(self):
        entries = {entry['name']: entry for entry in self.container['env']}
        self.assertEqual(entries['TELEGRAM_BOT_TOKEN_FILE']['value'], '/var/run/acorn/token')
        self.assertNotIn('TELEGRAM_BOT_TOKEN', entries)
        self.assertNotIn('envFrom', self.container)
        self.assertEqual(mounts(self.container)['/var/run/acorn'], {
            'name': 'token', 'mountPath': '/var/run/acorn', 'readOnly': True})
        secrets = [v for v in self.pod['volumes'] if 'secret' in v]
        self.assertEqual(secrets, [{'name': 'token', 'secret': {
            'secretName': 'acorn-care-bot', 'defaultMode': 0o440,
            'items': [{'key': 'bot-token', 'path': 'token'}]}}])
        for path in STANDALONE.glob('*.yaml'):
            self.assertFalse(any(obj['kind'] == 'Secret' for obj in documents(path.read_text())))

    def test_nonsecret_settings_use_a_hashed_generated_configmap(self):
        entries = {entry['name']: entry for entry in self.container['env']}
        settings = properties()
        self.assertEqual(set(settings), {'TELEGRAM_CHAT_ID', 'TELEGRAM_TOPIC_ID',
                                         'TELEGRAM_ALLOWED_USER_IDS', 'ACORN_FRESH_START'})
        self.assertEqual(set(entries), set(settings) | {
            'TELEGRAM_BOT_TOKEN_FILE', 'ACORN_CONFIG_PATH', 'ACORN_STATE_PATH'})
        for name in settings:
            self.assertEqual(entries[name], {'name': name, 'valueFrom': {
                'configMapKeyRef': {'name': 'acorn-care-bot-config', 'key': name}}})
        self.assertEqual(entries['ACORN_CONFIG_PATH']['value'], '/etc/acorn/config.json')
        self.assertEqual(mounts(self.container)['/etc/acorn'], {
            'name': 'configuration', 'mountPath': '/etc/acorn', 'readOnly': True})
        config_volume = next(v['configMap'] for v in self.pod['volumes'] if 'configMap' in v)
        self.assertEqual(config_volume, {'name': 'acorn-care-bot-config', 'defaultMode': 0o444,
                                         'items': [{'key': 'config.json', 'path': 'config.json'}]})
        kustomization = read(STANDALONE / 'kustomization.yaml')
        self.assertEqual(kustomization['configMapGenerator'], [{
            'name': 'acorn-care-bot-config', 'envs': ['runtime.properties'], 'files': ['config.json']}])
        self.assertFalse(kustomization.get('generatorOptions', {}).get('disableNameSuffixHash', False))
        self.assertNotIn('secretGenerator', kustomization)

    def test_existing_state_claim_is_retained(self):
        claim = read(STANDALONE / 'storage.yaml')
        self.assertEqual(claim['kind'], 'PersistentVolumeClaim')
        self.assertEqual(claim['metadata']['name'], 'acorn-care-bot-state')
        self.assertEqual(claim['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'], 'disabled')
        self.assertEqual(claim['spec']['accessModes'], ['ReadWriteOnce'])
        self.assertEqual(claim['spec']['storageClassName'], 'local-path')
        self.assertEqual(claim['spec']['resources']['requests']['storage'], '1Gi')
        self.assertEqual(next(v['persistentVolumeClaim'] for v in self.pod['volumes']
                              if 'persistentVolumeClaim' in v), {'claimName': 'acorn-care-bot-state'})
        state_env = next(entry for entry in self.container['env'] if entry['name'] == 'ACORN_STATE_PATH')
        self.assertEqual(state_env, {'name': 'ACORN_STATE_PATH', 'value': '/data/state.json'})
        self.assertEqual(mounts(self.container)['/data'], {'name': 'state', 'mountPath': '/data'})

    def test_security_and_readiness_are_preserved(self):
        self.assertIs(self.pod['automountServiceAccountToken'], False)
        self.assertIs(self.pod['securityContext']['runAsNonRoot'], True)
        for key in ('runAsUser', 'runAsGroup', 'fsGroup'):
            self.assertEqual(self.pod['securityContext'][key], 10001)
        self.assertEqual(self.pod['securityContext']['seccompProfile'], {'type': 'RuntimeDefault'})
        security = self.container['securityContext']
        self.assertIs(security['allowPrivilegeEscalation'], False)
        self.assertIs(security['readOnlyRootFilesystem'], True)
        self.assertEqual(security['capabilities']['drop'], ['ALL'])
        for probe, path in [('startupProbe', '/livez'), ('livenessProbe', '/livez'),
                            ('readinessProbe', '/readyz')]:
            self.assertEqual(self.container[probe]['httpGet'], {'path': path, 'port': 'health'})
        self.assertIn({'name': 'health', 'containerPort': 8080}, self.container['ports'])
        self.assertEqual(self.pod['terminationGracePeriodSeconds'], 75)
        self.assertFalse(any('hostPath' in v for v in self.pod['volumes']))

    def test_approved_configuration_loads_and_preserves_nutrition_plan(self):
        config = load_care_config(STANDALONE / 'config.json')
        plan = json.loads((ROOT / 'nutrition-plan-2026-11.json').read_text())
        self.assertEqual(config['nutrition_plan'], plan)
        self.assertEqual(config['nutrition_plan_id'], plan['id'])
        self.assertEqual(config['nutrition_notice_time'], plan['notice_time'])
        self.assertEqual(config['food_products'], plan['food_products'])
        self.assertIs(config['automatic_repeats'], False)
        self.assertEqual(config['food_transfer_policy'], 'add_full')
        self.assertEqual(config['mention'], '@palmette @LionZXY')
        self.assertEqual(properties(), {
            'TELEGRAM_CHAT_ID': '-1004424243504', 'TELEGRAM_TOPIC_ID': '210',
            'TELEGRAM_ALLOWED_USER_IDS': '474744725,1235433892', 'ACORN_FRESH_START': '1'})


@unittest.skipUnless(KUBECTL, 'kubectl is required; set KUBECTL_BIN')
class StandaloneRenderTests(unittest.TestCase):
    def render(self, path=STANDALONE):
        return documents(run([KUBECTL, 'kustomize', str(path)]))

    def config_name(self, objects):
        config, = [obj for obj in objects if obj['kind'] == 'ConfigMap']
        name = config['metadata']['name']
        self.assertRegex(name, r'^acorn-care-bot-config-[a-z0-9]+$')
        pod = deployment(objects)['spec']['template']['spec']
        for entry in pod['containers'][0]['env']:
            if 'valueFrom' in entry:
                self.assertEqual(entry['valueFrom']['configMapKeyRef']['name'], name)
        self.assertEqual(next(v['configMap']['name'] for v in pod['volumes'] if 'configMap' in v), name)
        return name

    def test_offline_render_has_only_namespaced_workload_config_and_storage(self):
        objects = self.render()
        self.assertCountEqual([obj['kind'] for obj in objects],
                              ['ConfigMap', 'Deployment', 'PersistentVolumeClaim'])
        self.assertTrue(all(obj['metadata']['namespace'] == NAMESPACE for obj in objects))
        self.config_name(objects)
        config = next(obj for obj in objects if obj['kind'] == 'ConfigMap')['data']
        self.assertEqual(set(config), set(properties()) | {'config.json'})
        self.assertEqual({key: config[key] for key in properties()}, properties())
        self.assertEqual(json.loads(config['config.json']), load_care_config(STANDALONE / 'config.json'))
        self.assertEqual(deployment(objects)['spec']['template']['spec']['containers'][0]['image'], PLACEHOLDER)

    def test_both_config_files_roll_the_generated_name_and_pod_references(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'standalone'
            shutil.copytree(STANDALONE, target)
            before = self.config_name(self.render(target))
            runtime = target / 'runtime.properties'
            runtime.write_text(runtime.read_text().replace('TELEGRAM_TOPIC_ID=210', 'TELEGRAM_TOPIC_ID=211'))
            after_runtime = self.config_name(self.render(target))
            self.assertNotEqual(before, after_runtime)
            path = target / 'config.json'
            config = json.loads(path.read_text())
            config['repeat_minutes'] += 1
            path.write_text(json.dumps(config))
            after_config = self.config_name(self.render(target))
            self.assertNotEqual(after_runtime, after_config)
            config['automatic_repeats'] = True
            path.write_text(json.dumps(config))
            self.assertNotEqual(after_config, self.config_name(self.render(target)))


class SuiteHomeProfileTests(unittest.TestCase):
    def setUp(self):
        self.release = read(SUITE / 'helmrelease.yaml')
        self.values = read(HOME)
        self.workers = {worker['id']: worker for worker in self.values['workers']}

    def test_release_uses_local_home_profile_and_shared_source_reference(self):
        self.assertEqual({path.name for path in SUITE.iterdir()}, {'helmrelease.yaml', 'kustomization.yaml'})
        self.assertEqual(read(SUITE / 'kustomization.yaml'), {
            'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
            'namespace': NAMESPACE, 'resources': ['helmrelease.yaml']})
        self.assertEqual(self.release['kind'], 'HelmRelease')
        self.assertEqual(self.release['metadata']['name'], 'telegram-bot-suite')
        spec = self.release['spec']
        self.assertEqual(spec['releaseName'], 'telegram-bot-suite')
        chart = spec['chart']['spec']
        self.assertEqual(chart['chart'], './charts/telegram-bot-suite')
        self.assertEqual(chart['reconcileStrategy'], 'Revision')
        self.assertEqual(chart['sourceRef'], {
            'kind': 'GitRepository', 'name': 'acorn-care-bot', 'namespace': 'flux-system'})
        self.assertEqual(chart['valuesFiles'], [
            './charts/telegram-bot-suite/values.yaml', './charts/telegram-bot-suite/values-home.yaml'])
        self.assertEqual(spec['values'], {'image': PLACEHOLDER})
        self.assertNotIn('valuesFrom', spec)
        self.assertNotIn('image', self.values)
        self.assertTrue(all('image' not in worker for worker in self.values['workers']))

    def test_suite_stays_suspended_with_zero_replicas_and_no_retry_activation(self):
        self.assertIs(self.release['spec']['suspend'], True)
        self.assertEqual(self.values['replicas'], 0)
        self.assertEqual(read(CHART / 'values.yaml')['replicas'], 0)
        for operation in ('install', 'upgrade'):
            self.assertEqual(self.release['spec'][operation]['remediation']['retries'], 0)
        self.assertTrue(self.release['spec']['postRenderers'])

    def test_home_profile_exactly_matches_standalone_care_and_route(self):
        self.assertEqual(set(self.workers), {'acorn', 'greenwich'})
        self.assertEqual(len(self.values['workers']), 2)
        acorn = self.workers['acorn']
        config = json.loads(acorn['config']['config.json'])
        validate_care_config(config)
        self.assertEqual(config, load_care_config(STANDALONE / 'config.json'))
        settings = properties()
        self.assertEqual(acorn['route'], {
            'chat_id': int(settings['TELEGRAM_CHAT_ID']), 'topic_id': int(settings['TELEGRAM_TOPIC_ID']),
            'receives_updates': True,
            'allowed_user_ids': [int(uid) for uid in settings['TELEGRAM_ALLOWED_USER_IDS'].split(',')],
            'methods': ['getMe', 'getWebhookInfo', 'getChatMember', 'getUpdates',
                        'sendMessage', 'editMessageText', 'answerCallbackQuery']})
        self.assertEqual(acorn['env'], {'ACORN_CONFIG_PATH': '/etc/worker/config.json',
                                        'ACORN_STATE_PATH': '/data/state.json',
                                        'ACORN_FRESH_START': settings['ACORN_FRESH_START']})
        self.assertEqual(acorn['persistence'], {'existingClaim': 'acorn-care-bot-state'})
        self.assertEqual(acorn['command'], ['python3', '/app/service.py'])
        self.assertEqual(self.values['legacyAcorn'], {'workerId': 'acorn'})
        self.assertEqual((acorn['uid'], self.workers['greenwich']['uid']), (10001, 10002))

    def test_greenwich_permissions_and_secret_references_are_preserved(self):
        greenwich = self.workers['greenwich']
        self.assertEqual(greenwich['route'], {
            'chat_id': -1004424243504, 'topic_id': 45, 'receives_updates': False,
            'allowed_user_ids': [], 'methods': ['getMe', 'getChatMember', 'sendMessage',
                'sendRichMessage', 'pinChatMessage', 'unpinChatMessage', 'deleteMessage']})
        self.assertEqual(greenwich['command'], ['python3', '/app/greenwich/runner.py'])
        self.assertEqual(greenwich['secretMounts'], [{
            'secretName': 'greenwich-bin-bot-property', 'mountPath': '/etc/bins',
            'items': [{'key': 'property.json', 'path': 'property.json'}]}])
        self.assertEqual(self.values['gateway']['tokenSecret'], {'name': 'acorn-care-bot', 'key': 'bot-token'})
        self.assertEqual(self.values['gateway']['persistence']['storageClassName'], 'local-path')
        for worker in self.workers.values():
            self.assertFalse(any('TOKEN' in name.upper() for name in worker['env']))
            self.assertNotIn('secretKeyRef', json.dumps(worker))
            self.assertNotIn('envFrom', worker)
        self.assertFalse(greenwich.get('config'))

    @unittest.skipUnless(KUBECTL, 'kubectl is required; set KUBECTL_BIN')
    def test_suite_kustomization_renders_only_the_suspended_namespaced_release(self):
        objects = documents(run([KUBECTL, 'kustomize', str(SUITE)]))
        self.assertEqual(len(objects), 1)
        expected = copy.deepcopy(self.release)
        expected['metadata']['namespace'] = NAMESPACE
        self.assertEqual(objects[0], expected)


@unittest.skipUnless(HELM, 'Helm is required; set HELM_BIN')
class HomeChartRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = read(SUITE / 'helmrelease.yaml')
        cls.profile = read(HOME)
        with tempfile.TemporaryDirectory() as directory:
            overrides = Path(directory) / 'release-values.yaml'
            overrides.write_text(yaml.safe_dump(cls.release['spec']['values']))
            command = [HELM, 'template', cls.release['spec']['releaseName'], str(CHART),
                       '--namespace', NAMESPACE]
            for path in cls.release['spec']['chart']['spec']['valuesFiles']:
                command += ['--values', str(ROOT / path)]
            command += ['--values', str(overrides)]
            cls.objects = documents(run(command))
        cls.deployment = deployment(cls.objects)
        cls.pod = cls.deployment['spec']['template']['spec']
        cls.containers = {c['name']: c for c in cls.pod['containers']}

    def test_home_renders_offline_with_inactive_single_writer_and_placeholder_images(self):
        self.assertEqual(self.deployment['metadata']['name'], 'telegram-bot-suite')
        self.assertEqual(self.deployment['spec']['replicas'], 0)
        self.assertEqual(self.deployment['spec']['strategy'], {'type': 'Recreate'})
        self.assertEqual(set(self.containers), {'acorn', 'greenwich', 'gateway'})
        self.assertEqual({obj['kind'] for obj in self.objects}, {
            'Deployment', 'ConfigMap', 'PersistentVolumeClaim', 'NetworkPolicy'})
        for container in self.pod['containers'] + self.pod['initContainers']:
            self.assertEqual(container['image'], PLACEHOLDER)

    def test_rendered_registry_passes_gateway_validation_and_matches_profile(self):
        maps = {obj['metadata']['name']: obj['data'] for obj in self.objects if obj['kind'] == 'ConfigMap'}
        registry = json.loads(maps['telegram-bot-suite-registry']['registry.json'])
        validated = validate_config(registry)
        self.assertEqual(len(validated['workers']), 2)
        self.assertEqual(registry['state_path'], '/gateway/private/state.sqlite3')
        self.assertEqual(env(self.containers['gateway'])['GATEWAY_LEGACY_PATH'], '/gateway/legacy.json')
        expected = [dict(worker['route'], id=worker['id'], uid=worker['uid'],
                         socket_path=f"/run/workers/{worker['id']}/private/api.sock")
                    for worker in self.profile['workers']]
        self.assertEqual(registry['workers'], expected)
        for worker in self.profile['workers']:
            container = self.containers[worker['id']]
            self.assertEqual(maps[f"telegram-bot-suite-{worker['id']}-config"], worker.get('config', {}))
            settings = env(container)
            self.assertEqual(settings['TELEGRAM_CHAT_ID'], str(worker['route']['chat_id']))
            self.assertEqual(settings['TELEGRAM_TOPIC_ID'], str(worker['route']['topic_id']))
            self.assertEqual(settings['TELEGRAM_ALLOWED_USER_IDS'],
                             ','.join(map(str, worker['route']['allowed_user_ids'])))
            self.assertEqual(settings['TELEGRAM_GATEWAY_SOCKET'], '/run/telegram/private/api.sock')
            for name, value in worker['env'].items():
                self.assertEqual(settings[name], value)
            for probe in ('startupProbe', 'livenessProbe', 'readinessProbe'):
                self.assertEqual(container[probe], worker[probe])
        self.assertEqual(self.containers['gateway']['readinessProbe']['httpGet'], {
            'path': '/readyz', 'port': 'gateway-health'})

    def test_workers_are_isolated_from_each_other_and_gateway_credentials(self):
        volumes = {v['name']: v for v in self.pod['volumes']}
        self.assertEqual(len(volumes), len(self.pod['volumes']))
        secrets = {name: v['secret'] for name, v in volumes.items() if 'secret' in v}
        self.assertEqual(secrets, {
            'telegram-token': {'secretName': 'acorn-care-bot', 'defaultMode': 0o440,
                               'items': [{'key': 'bot-token', 'path': 'token'}]},
            'worker-1-secret-0': {'secretName': 'greenwich-bin-bot-property', 'defaultMode': 0o440,
                                  'items': [{'key': 'property.json', 'path': 'property.json'}]}})
        worker_mounts = []
        for worker in self.profile['workers']:
            container = self.containers[worker['id']]
            self.assertEqual(container['securityContext']['runAsUser'], worker['uid'])
            paths = mounts(container)
            names = {m['name'] for m in paths.values()}
            worker_mounts.append(names)
            self.assertNotIn('telegram-token', names)
            self.assertNotIn('gateway-state', names)
            self.assertNotIn('/gateway', paths)
            self.assertFalse(any('TOKEN' in name.upper() for name in env(container)))
            for path in ('/etc/worker', '/run/telegram'):
                self.assertIs(paths[path]['readOnly'], True)
            self.assertEqual(volumes[paths['/run/telegram']['name']]['emptyDir'], {
                'medium': 'Memory', 'sizeLimit': '1Mi'})
        self.assertTrue(worker_mounts[0].isdisjoint(worker_mounts[1]))
        self.assertNotIn('worker-1-secret-0', worker_mounts[0])
        self.assertIs(mounts(self.containers['greenwich'])['/etc/bins']['readOnly'], True)
        gateway = self.containers['gateway']
        self.assertEqual(gateway['securityContext']['runAsUser'], 10003)
        self.assertEqual(env(gateway)['TELEGRAM_BOT_TOKEN_FILE'], '/var/run/telegram/token')
        self.assertIs(mounts(gateway)['/var/run/telegram']['readOnly'], True)
        self.assertNotIn('/data', mounts(gateway))
        self.assertNotIn('/etc/bins', mounts(gateway))
        for worker in self.profile['workers']:
            self.assertIn('/run/workers/' + worker['id'], mounts(gateway))

    def test_all_containers_are_hardened_without_inline_secrets(self):
        self.assertIs(self.pod['automountServiceAccountToken'], False)
        self.assertIs(self.pod['shareProcessNamespace'], False)
        self.assertNotIn('imagePullSecrets', self.pod)
        self.assertFalse(any('hostPath' in v for v in self.pod['volumes']))
        for container in self.pod['containers'] + self.pod['initContainers']:
            security = container['securityContext']
            self.assertIs(security['runAsNonRoot'], True)
            self.assertIs(security['readOnlyRootFilesystem'], True)
            self.assertIs(security['allowPrivilegeEscalation'], False)
            self.assertEqual(security['capabilities'], {'drop': ['ALL']})
            self.assertEqual(security['seccompProfile'], {'type': 'RuntimeDefault'})
            self.assertNotIn('envFrom', container)
            for entry in container['env']:
                self.assertEqual(set(entry), {'name', 'value'})
                self.assertIsInstance(entry['value'], str)
                self.assertNotEqual(entry['name'], 'TELEGRAM_BOT_TOKEN')
        for obj in self.objects:
            self.assertNotEqual(obj['kind'], 'Secret')
            self.assertNotIn('stringData', obj)
            self.assertNotIn('binaryData', obj)
        self.assertNotRegex(json.dumps(self.objects), r'\b[0-9]+:[A-Za-z0-9_-]{16,}\b')

    def test_storage_names_retention_and_read_only_legacy_seed_are_preserved(self):
        claims = {obj['metadata']['name']: obj for obj in self.objects if obj['kind'] == 'PersistentVolumeClaim'}
        self.assertEqual(set(claims), {'telegram-bot-suite-gateway-state', 'telegram-bot-suite-greenwich-state'})
        for claim in claims.values():
            self.assertEqual(claim['metadata']['annotations']['helm.sh/resource-policy'], 'keep')
            self.assertEqual(claim['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'], 'disabled')
            self.assertEqual(claim['spec']['accessModes'], ['ReadWriteOnce'])
            self.assertEqual(claim['spec']['storageClassName'], 'local-path')
            self.assertEqual(claim['spec']['resources']['requests']['storage'], '128Mi')
        volumes = {v['name']: v for v in self.pod['volumes']}
        self.assertEqual({name: v['persistentVolumeClaim']['claimName'] for name, v in volumes.items()
                          if 'persistentVolumeClaim' in v}, {
            'gateway-state': 'telegram-bot-suite-gateway-state', 'worker-0-state': 'acorn-care-bot-state',
            'worker-1-state': 'telegram-bot-suite-greenwich-state'})
        seed, = self.pod['initContainers']
        self.assertEqual(seed['name'], 'seed-gateway')
        self.assertEqual(seed['command'], ['python3', '/app/gateway_seed.py'])
        self.assertEqual(seed['securityContext']['runAsUser'], 10001)
        self.assertEqual(set(mounts(seed)), {'/legacy', '/gateway'})
        self.assertEqual(mounts(seed)['/legacy'], {
            'name': 'worker-0-state', 'mountPath': '/legacy', 'readOnly': True})
        self.assertEqual(env(seed)['TELEGRAM_CHAT_ID'], properties()['TELEGRAM_CHAT_ID'])
        self.assertEqual(env(seed)['TELEGRAM_TOPIC_ID'], properties()['TELEGRAM_TOPIC_ID'])

    @unittest.skipUnless(KUBECTL, 'kubectl is required; set KUBECTL_BIN')
    def test_flux_postrenderers_remove_memory_fields_and_nothing_else(self):
        expected = copy.deepcopy(self.objects)
        expected_pod = deployment(expected)['spec']['template']['spec']
        expected_containers = expected_pod['containers'] + expected_pod['initContainers']
        self.assertEqual({c['name'] for c in expected_containers}, {'acorn', 'greenwich', 'gateway', 'seed-gateway'})
        for container in expected_containers:
            for category in ('requests', 'limits'):
                self.assertIn('memory', container['resources'][category])
                del container['resources'][category]['memory']
        rendered = copy.deepcopy(self.objects)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for renderer in self.release['spec']['postRenderers']:
                self.assertEqual(set(renderer), {'kustomize'})
                (target / 'rendered.yaml').write_text(yaml.safe_dump_all(rendered))
                (target / 'kustomization.yaml').write_text(yaml.safe_dump({
                    'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
                    'namespace': NAMESPACE, 'resources': ['rendered.yaml'], **renderer['kustomize']}))
                rendered = documents(run([KUBECTL, 'kustomize', str(target)]))
        # Helm installs into the release namespace; apply it to the offline output too.
        for obj in expected:
            obj['metadata']['namespace'] = NAMESPACE
        key = lambda obj: (obj['kind'], obj['metadata']['name'])
        self.assertEqual(sorted(rendered, key=key), sorted(expected, key=key))
        actual_pod = deployment(rendered)['spec']['template']['spec']
        for container in actual_pod['containers'] + actual_pod['initContainers']:
            for category in ('requests', 'limits'):
                self.assertNotIn('memory', container['resources'][category])
                self.assertIn('cpu', container['resources'][category])
        self.assertEqual(deployment(rendered)['spec']['replicas'], 0)


if __name__ == '__main__':
    unittest.main()
