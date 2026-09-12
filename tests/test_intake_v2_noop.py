"""Общий ранний no-op и границы неизменности трёх транспортов."""
import io
import os
import zipfile
from pathlib import Path
from dataclasses import replace
import pytest
from conftest import build_configuration, write_export
from test_intake_v2_collector import _configuration
from test_intake_v2_converter import _xdto_descriptor
from mcp1c.registry import Registry
from mcp1c.intake_v2_api import IntakeApiConflict, IntakeApiService
from mcp1c.intake_v2 import ExportIdentity
import mcp1c.intake_v2_operations as ops
from mcp1c.tools import get_object


def _xdto_qname_archive(reference_namespace):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        bundle.writestr('Configuration.xml', _configuration('QNameUpdate'))
        for name, namespace in [('First', 'urn:first'), ('Second', 'urn:second')]:
            bundle.writestr(
                f'XDTOPackages/{name}.xml',
                _xdto_descriptor(name, namespace),
            )
            bundle.writestr(
                f'XDTOPackages/{name}/Ext/Package.bin',
                f'<package xmlns="http://v8.1c.ru/8.1/xdto" '
                f'targetNamespace="{namespace}"><objectType name="Thing"/>'
                '</package>',
            )
        bundle.writestr(
            'XDTOPackages/Main.xml',
            _xdto_descriptor('Main', 'urn:main'),
        )
        bundle.writestr(
            'XDTOPackages/Main/Ext/Package.bin',
            f'<package xmlns="http://v8.1c.ru/8.1/xdto" '
            f'xmlns:t="{reference_namespace}" targetNamespace="urn:main">'
            '<import namespace="urn:first"/><import namespace="urn:second"/>'
            '<property name="Global" type="t:Thing"/></package>',
        )
    return stream.getvalue()


@pytest.fixture(params=['directory', 'incoming', 'browser'])
def world(tmp_path, request):
    root = tmp_path/'mounts'/'config-test'; root.mkdir(parents=True)
    (root/'Configuration.xml').write_bytes(_configuration('Demo0'))
    module = root/'CommonModules'/'Demo'/'Ext'/'Module.bsl'; module.parent.mkdir(parents=True)
    module.write_text('Процедура Проверка() Экспорт\n Сообщить("до");\nКонецПроцедуры', encoding='utf-8')
    registry = Registry(tmp_path/'data')
    seed=tmp_path/'seed';seed.mkdir()
    registry.add_configuration(write_export(seed,build_configuration(name='Demo0')),keep_source=False)
    service=IntakeApiService.for_registry(registry,config_sources_root=root.parent,directory_settle_seconds=0)
    transport=request.param
    if transport=='directory': service.bind_directory('Demo0','config-test')
    def archive():
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            for p in sorted(root.rglob('*')):
                if p.is_file(): z.writestr(zipfile.ZipInfo(p.relative_to(root).as_posix()),p.read_bytes())
        return data.getvalue()
    def candidate():
        if transport=='directory': return service.refresh_directory('Demo0')
        data=archive()
        if transport=='browser': return service.accept_upload('source.zip',io.BytesIO(data),expected_size=len(data))
        registry.incoming_dir.mkdir(exist_ok=True)
        archive_path=registry.incoming_dir/'source.zip'
        if not archive_path.exists() or archive_path.read_bytes()!=data: archive_path.write_bytes(data)
        return service.snapshot()['candidates'][0]
    def prepare():
        c=candidate(); w=service.start(c['id'],'update_full');service.prepare(w);return w
    w=prepare();service.confirm(w.job_id)
    return locals()

def test_noop_skips_collection_and_survives_restart(world, monkeypatch):
    d=world
    before=d['registry'].active_generation_pointer(ExportIdentity.configuration('Demo0'))
    monkeypatch.setattr(ops,'collect_source_b',lambda *a,**kw:pytest.fail('Неизменённый вход повторно разбирается'))
    w=d['prepare']()
    assert d['service'].job_payload(w.job_id)['preview']['no_op']
    recovered=Registry(d['registry'].data_dir)
    recovered.startup()
    assert recovered.wait_for_module_builds()
    assert recovered.active_generation_pointer(ExportIdentity.configuration('Demo0'))==before
    restarted=IntakeApiService.for_registry(recovered,config_sources_root=d['root'].parent,directory_settle_seconds=0)
    assert restarted.confirm(w.job_id)['commit']['no_op']
    assert d['registry'].active_generation_pointer(ExportIdentity.configuration('Demo0'))==before


def test_two_browser_noop_previews_confirm_after_cleanup_and_restart(tmp_path):
    registry = Registry(tmp_path / 'data')
    service = IntakeApiService.for_registry(registry, directory_settle_seconds=0)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('Configuration.xml', _configuration('Demo0'))
    raw = payload.getvalue()
    initial_candidate = service.accept_upload(
        'source.zip', io.BytesIO(raw), expected_size=len(raw)
    )
    initial = service.start(initial_candidate['id'], 'create')
    service.prepare(initial)
    assert not service.confirm(initial.job_id)['commit']['no_op']

    candidate = service.accept_upload(
        'source.zip', io.BytesIO(raw), expected_size=len(raw)
    )

    works = [service.start(candidate['id'], 'update_full') for _ in range(2)]
    for work in works:
        service.prepare(work)
        assert service.job_payload(work.job_id)['preview']['no_op']

    assert service.confirm(works[0].job_id)['commit']['no_op']
    assert service.job_payload(works[1].job_id)['preview']['no_op']

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restarted_service = IntakeApiService.for_registry(
        restarted, directory_settle_seconds=0
    )
    second = restarted_service.confirm(works[1].job_id)
    assert second['commit']['no_op']
    assert restarted_service.confirm(works[1].job_id) == second


@pytest.mark.parametrize('world', ['directory'], indirect=True)
def test_directory_preview_rejects_changed_binding(world):
    service = world['service']
    work = world['prepare']()
    assert service.job_payload(work.job_id)['preview']['no_op']

    service.unbind_directory('Demo0')

    with pytest.raises(
        IntakeApiConflict,
        match='Привязка каталога конфигурации изменилась',
    ):
        service.confirm(work.job_id)


@pytest.mark.parametrize('change',['same-stat','add','delete','rename'])
def test_real_changes_use_full_path(world,monkeypatch,change):
    d=world;m=d['module'];old=m.stat()
    if change=='same-stat':
        m.write_bytes(m.read_bytes().replace('до'.encode(),'да'.encode()))
        os.utime(m,ns=(old.st_atime_ns,old.st_mtime_ns))
    elif change=='add':
        p=d['root']/'CommonModules'/'Other'/'Ext'/'Module.bsl';p.parent.mkdir(parents=True);p.write_bytes(m.read_bytes())
    elif change=='delete': m.unlink()
    else: m.rename(m.with_name('ObjectModule.bsl'))
    calls=[];original=ops.collect_source_b
    def collect(*a,**kw): calls.append(1);return original(*a,**kw)
    monkeypatch.setattr(ops,'collect_source_b',collect)
    w=d['prepare']()
    assert calls
    assert not d['service'].job_payload(w.job_id)['preview']['no_op']

@pytest.mark.parametrize('stage',['before-prepare','after-preview'])
def test_selected_input_change_boundary(world,stage):
    d=world;s=d['service'];c=d['candidate']();w=s.start(c['id'],'update_full')
    if stage=='after-preview': s.prepare(w)
    d['module'].write_bytes(d['module'].read_bytes().replace('до'.encode(),'да'.encode()))
    if d['transport']=='directory':
        # Проверяем также сохранение stat при изменении содержимого на месте.
        path=d['module']
    elif d['transport']=='incoming': path=d['registry'].incoming_dir/'source.zip'
    else:
        locator=s.lifecycle.catalog.load(c['id']).locator
        path=s.lifecycle.browser._payload_path(locator.entry_name)
    if d['transport']!='directory':
        info=path.stat();path.write_bytes(d['archive']());os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
    if stage=='before-prepare' or d['transport']=='directory':
        with pytest.raises(Exception):
            if stage=='before-prepare': s.prepare(w)
            else: s.confirm(w.job_id)
    else:
        # Для ZIP подтверждается уже подготовленный снимок, не новая версия ZIP.
        assert s.confirm(w.job_id)['commit']['no_op']

@pytest.mark.parametrize('kind',['identity','transport','origin','parser','selection','action','overlay'])
def test_fast_path_requires_complete_matching_provenance(world,kind):
    from mcp1c.intake_v2 import CandidateTransport
    from mcp1c.intake_v2_planner import IntakeAction
    from mcp1c.intake_v2_registry import native_generation_view
    d=world;s=d['service'];c=d['candidate']();w=s.start(c['id'],'update_full')
    candidate=s.lifecycle.operations.records.load_candidate(c['id']);active=w.active;action=w.action
    assert ops._reuse_active(candidate,active,action)
    if kind=='identity': candidate=replace(candidate,identity=ExportIdentity.configuration('Other'))
    elif kind=='transport': candidate=replace(candidate,transport=CandidateTransport.LOCAL_FILE)
    elif kind=='origin': candidate=replace(candidate,origin_name='other.zip')
    elif kind=='parser': active=native_generation_view(replace(active.manifest,parser_version=active.manifest.parser_version-1))
    elif kind=='selection': active=native_generation_view(replace(active.manifest,selection_version=active.manifest.selection_version-1))
    elif kind=='action': action=IntakeAction.UPDATE_CONTENT
    else:
        d['registry'].add_configuration(write_export(d['tmp_path']/'seed',build_configuration(name='Demo0',version='2.0')),keep_source=False)
        active=d['registry'].generation_view('Demo0')
    assert not ops._reuse_active(candidate,active,action)

def test_new_active_generation_rejects_stale_noop(world):
    d=world;w=d['prepare']()
    d['module'].write_bytes(d['module'].read_bytes().replace('до'.encode(),'да'.encode()))
    changed=d['prepare']();d['service'].confirm(changed.job_id)
    jobs = d['service'].jobs_snapshot()['jobs']
    assert next(item for item in jobs if item['job_id'] == w.job_id)['state'] == 'failed'
    with pytest.raises(Exception): d['service'].confirm(w.job_id)

def test_directory_probe_uses_two_scans(tmp_path,monkeypatch):
    import mcp1c.intake_v2_transport as transport
    from mcp1c.intake_v2_probe import probe_export
    (tmp_path/'Configuration.xml').write_bytes(_configuration('Demo0'))
    original=transport._scan_directory;calls=[]
    def scan(*a,**kw):calls.append(1);return original(*a,**kw)
    monkeypatch.setattr(transport,'_scan_directory',scan)
    tree=transport.DirectoryExportTree(tmp_path,settle_seconds=0)
    probe_export(tree)
    assert len(calls)==2
    # Готовый хеш не отменяет новой проверки стабильности при повторном probe.
    (tmp_path/'new.txt').write_text('changed')
    with pytest.raises(Exception):probe_export(tree)

def test_unchanged_bytes_rewritten_can_be_prepared(world):
    d=world
    if d['transport']=='browser': pytest.skip('Новая браузерная загрузка получает новый id')
    p=d['module'] if d['transport']=='directory' else d['registry'].incoming_dir/'source.zip'
    st=p.stat();os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns+1000000))
    w = d['prepare']()
    assert d['service'].job_payload(w.job_id)['preview']['no_op']
    assert d['service'].confirm(w.job_id)['commit']['no_op']


def test_xdto_qname_namespace_не_теряется_в_noop_после_restart(tmp_path):
    registry = Registry(tmp_path / 'data')
    service = IntakeApiService.for_registry(registry, directory_settle_seconds=0)

    def prepare(payload, action):
        candidate = service.accept_upload(
            'source.zip', io.BytesIO(payload), expected_size=len(payload)
        )
        work = service.start(candidate['id'], action)
        service.prepare(work)
        return work

    initial_payload = _xdto_qname_archive('urn:first')
    initial = prepare(initial_payload, 'create')
    assert not service.job_payload(initial.job_id)['preview']['no_op']
    assert not service.confirm(initial.job_id)['commit']['no_op']

    changed_payload = _xdto_qname_archive('urn:second')
    changed = prepare(changed_payload, 'update_full')
    assert not service.job_payload(changed.job_id)['preview']['no_op']
    assert not service.confirm(changed.job_id)['commit']['no_op']

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    card = get_object(
        restarted,
        'ПакетXDTO.Main.Свойство.Global',
        config='QNameUpdate',
        detail='full',
    )
    assert 'ПакетXDTO.Second.ТипОбъекта.Thing' in card
    assert 'ПакетXDTO.First.ТипОбъекта.Thing' not in card

    restarted_service = IntakeApiService.for_registry(
        restarted, directory_settle_seconds=0
    )
    candidate = restarted_service.accept_upload(
        'source.zip',
        io.BytesIO(changed_payload),
        expected_size=len(changed_payload),
    )
    repeated = restarted_service.start(candidate['id'], 'update_full')
    restarted_service.prepare(repeated)
    assert restarted_service.job_payload(repeated.job_id)['preview']['no_op']
    assert restarted_service.confirm(repeated.job_id)['commit']['no_op']


def test_xdto_старое_поколение_читается_и_требует_явный_reparse(
    tmp_path, monkeypatch
):
    from mcp1c import intake_v2_generation

    payload = _xdto_qname_archive('urn:first')
    registry = Registry(tmp_path / 'data')
    with monkeypatch.context() as legacy:
        legacy.setattr(
            intake_v2_generation,
            'GENERATION_PARSER_VERSION',
            intake_v2_generation.GENERATION_PARSER_VERSION - 1,
        )
        service = IntakeApiService.for_registry(
            registry, directory_settle_seconds=0
        )
        candidate = service.accept_upload(
            'source.zip', io.BytesIO(payload), expected_size=len(payload)
        )
        initial = service.start(candidate['id'], 'create')
        service.prepare(initial)
        assert not service.confirm(initial.job_id)['commit']['no_op']

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert 'ПакетXDTO.First.ТипОбъекта.Thing' in get_object(
        restarted,
        'ПакетXDTO.Main.Свойство.Global',
        config='QNameUpdate',
        detail='full',
    )

    calls = []
    collect = ops.collect_source_b

    def tracked_collect(*args, **kwargs):
        calls.append(1)
        return collect(*args, **kwargs)

    monkeypatch.setattr(ops, 'collect_source_b', tracked_collect)
    service = IntakeApiService.for_registry(restarted, directory_settle_seconds=0)
    candidate = service.accept_upload(
        'source.zip', io.BytesIO(payload), expected_size=len(payload)
    )
    update = service.start(candidate['id'], 'update_full')
    service.prepare(update)

    assert calls
    assert not service.job_payload(update.job_id)['preview']['no_op']
    assert not service.confirm(update.job_id)['commit']['no_op']
    manifest = restarted.active_generation(
        ExportIdentity.configuration('QNameUpdate')
    )
    assert manifest is not None
    assert manifest.parser_version == intake_v2_generation.GENERATION_PARSER_VERSION


def test_reexport_keeps_previous_durable_probe(world):
    d = world
    previous = d['prepare']()
    records = d['service'].lifecycle.operations.records
    candidate = records.load_candidate(previous.candidate_id)
    if d['transport'] != 'browser':
        path = (d['module'] if d['transport'] == 'directory'
                else d['registry'].incoming_dir / 'source.zip')
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    current = d['prepare']()
    assert current.candidate_id != previous.candidate_id
    assert records.load_candidate(previous.candidate_id) == candidate
    assert d['service'].confirm(current.job_id)['commit']['no_op']


@pytest.mark.parametrize('damage', ['flag', 'parser'])
def test_compact_preview_is_revalidated(world, damage, monkeypatch):
    import json
    from mcp1c import intake_v2_generation
    d = world
    work = d['prepare']()
    coordinator = d['service'].lifecycle.operations
    if damage == 'flag':
        path = coordinator.previews_dir / (work.job_id + '.json')
        payload = json.loads(path.read_text())
        payload['payload']['reuse_active'] = 'true'
        path.write_text(json.dumps(payload))
    else:
        monkeypatch.setattr(intake_v2_generation, 'GENERATION_PARSER_VERSION',
                            intake_v2_generation.GENERATION_PARSER_VERSION + 1)
    with pytest.raises(ops.OperationError):
        coordinator.load_preview(work.job_id)
