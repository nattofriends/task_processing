import threading
import time

import mock
import pytest
from addict import Dict
from pyrsistent import v

from task_processing.plugins.mesos import execution_framework as ef_mdl
from task_processing.plugins.mesos import mesos_executor as me_mdl


@pytest.fixture
def mock_Thread():
    with mock.patch.object(threading, 'Thread') as mock_Thread:
        yield mock_Thread


@pytest.fixture
def ef(mock_Thread):
    return ef_mdl.ExecutionFramework("fake_name", "fake_role")


@pytest.fixture
def fake_task():
    return me_mdl.MesosTaskConfig(
        name='fake_name',
        cpus=10.0,
        mem=1024.0,
        disk=1000.0,
        image='fake_image',
        cmd='echo "fake"'
    )


@pytest.fixture
def fake_offer():
    return Dict(
        id=Dict(value='fake_offer_id'),
        agent_id=Dict(value='fake_agent_id'),
        hostname='fake_hostname',
        resources=[
            Dict(
                role='fake_role',
                name='cpus',
                scalar=Dict(value=10),
                type='SCALAR',
            ),
            Dict(
                role='fake_role',
                name='mem',
                scalar=Dict(value=1024),
                type='SCALAR',
            ),
            Dict(
                role='fake_role',
                name='disk',
                scalar=Dict(value=1000),
                type='SCALAR',
            ),
            Dict(
                role='fake_role',
                name='ports',
                ranges=Dict(range=[Dict(begin=31200, end=31500)]),
                type='RANGES',
            ),
        ],
        attributes=[
            Dict(
                name='pool',
                text=Dict(value='fake_pool_text')
            )
        ]
    )


@pytest.fixture
def fake_driver():
    fake_driver = mock.Mock(spec=[
        'id',
        'declineOffer',
        'suppressOffers',
        'reviveOffers',
        'launchTasks',
        'killTask',
        'acknowledgeStatusUpdate'
    ])
    fake_driver.id = 'fake_driver'

    return fake_driver


@pytest.fixture
def mock_get_metric():
    with mock.patch.object(ef_mdl, 'get_metric') as mock_get_metric:
        yield mock_get_metric


@pytest.fixture
def mock_time():
    with mock.patch.object(time, 'time') as mock_time:
        yield mock_time


@pytest.fixture
def mock_sleep(ef):
    def stop_killing(task_id):
        ef.stopping = True

    with mock.patch.object(time, 'sleep', side_effect=stop_killing) as\
            mock_sleep:
        yield mock_sleep


def test_ef_kills_stuck_tasks(
    ef,
    fake_task,
    mock_sleep,
    mock_get_metric
):
    task_id = fake_task.task_id
    task_metadata = ef_mdl.TaskMetadata(
        agent_id='fake_agent_id',
        task_config=fake_task,
        task_state='TASK_STAGING',
        task_state_ts=0.0,
    )
    ef.task_staging_timeout_s = 0
    ef.kill_task = mock.Mock()
    ef.blacklist_slave = mock.Mock()

    ef.task_metadata = ef.task_metadata.set(task_id, task_metadata)
    ef.kill_tasks_stuck_in_staging()

    assert ef.kill_task.call_count == 1
    assert ef.kill_task.call_args == mock.call(task_id)
    assert ef.blacklist_slave.call_count == 1
    assert ef.blacklist_slave.call_args == mock.call(
        agent_id='fake_agent_id',
        timeout=900
    )
    assert mock_get_metric.call_count == 1
    assert mock_get_metric.call_args == mock.call(ef_mdl.TASK_STUCK_COUNT)
    assert mock_get_metric.return_value.count.call_count == 1
    assert mock_get_metric.return_value.count.call_args == mock.call(1)


def test_offer_matches_pool_no_pool(ef, fake_offer):
    assert ef.offer_matches_pool(fake_offer)


def test_offer_matches_pool_match(ef, fake_offer):
    ef.pool = 'fake_pool_text'

    assert ef.offer_matches_pool(fake_offer)


def test_offer_matches_pool_no_match(ef, fake_offer):
    ef.pool = 'fake_other_pool_text'

    assert not ef.offer_matches_pool(fake_offer)


def test_kill_task(ef, fake_driver):
    ef.driver = fake_driver

    ef.kill_task('fake_task_id')

    assert fake_driver.killTask.call_count == 1
    assert fake_driver.killTask.call_args == mock.call(
        Dict(value='fake_task_id')
    )


def test_blacklist_slave(
    ef,
    mock_get_metric,
    mock_time
):
    agent_id = 'fake_agent_id'
    mock_time.return_value = 2.0

    ef.blacklisted_slaves = ef.blacklisted_slaves.append(agent_id)
    ef.blacklist_slave(agent_id, timeout=2.0)

    assert agent_id in ef.blacklisted_slaves
    assert mock_get_metric.call_count == 1
    assert mock_get_metric.call_args == mock.call(
        ef_mdl.BLACKLISTED_AGENTS_COUNT
    )
    assert mock_get_metric.return_value.count.call_count == 1
    assert mock_get_metric.return_value.count.call_args == mock.call(1)


def test_unblacklist_slave(
    ef,
    mock_time,
    mock_sleep
):
    agent_id = 'fake_agent_id'

    ef.blacklisted_slaves = ef.blacklisted_slaves.append(agent_id)
    ef.unblacklist_slave(agent_id, timeout=0.0)

    assert agent_id not in ef.blacklisted_slaves


def test_enqueue_task(
    ef,
    fake_task,
    fake_driver,
    mock_get_metric
):
    ef.are_offers_suppressed = True
    ef.driver = fake_driver

    ef.enqueue_task(fake_task)

    assert ef.task_metadata[fake_task.task_id].task_state == 'TASK_INITED'
    assert not ef.task_queue.empty()
    assert ef.driver.reviveOffers.call_count == 1
    assert not ef.are_offers_suppressed
    assert mock_get_metric.call_count == 1
    assert mock_get_metric.call_args == mock.call(ef_mdl.TASK_ENQUEUED_COUNT)
    assert mock_get_metric.return_value.count.call_count == 1
    assert mock_get_metric.return_value.count.call_args == mock.call(1)


def test_get_available_ports(ef, fake_offer):
    ports_resource = [r for r in fake_offer.resources if r.name is 'ports'][0]

    ports = ef.get_available_ports(ports_resource)

    for p in range(31200, 31500):
        assert p in ports


def test_get_tasks_to_launch_sufficient_offer(
    ef,
    fake_task,
    fake_offer,
    mock_get_metric,
    mock_time
):
    task_metadata = ef_mdl.TaskMetadata(
        task_config=fake_task,
        task_state='fake_state',
        task_state_ts=1.0
    )
    ef.create_new_docker_task = mock.Mock()
    mock_time.return_value = 2.0

    ef.task_queue.put(fake_task)
    ef.task_metadata = ef.task_metadata.set(fake_task.task_id, task_metadata)
    tasks_to_launch = ef.get_tasks_to_launch(fake_offer)

    assert ef.create_new_docker_task.return_value in tasks_to_launch
    assert ef.task_queue.qsize() == 0
    assert mock_get_metric.call_count == 1
    assert mock_get_metric.call_args == mock.call(
        ef_mdl.TASK_QUEUED_TIME_TIMER
    )
    assert mock_get_metric.return_value.record.call_count == 1
    assert mock_get_metric.return_value.record.call_args == mock.call(1.0)


def test_get_tasks_to_launch_insufficient_offer(
    ef,
    fake_offer,
    mock_get_metric
):
    ef.create_new_docker_task = mock.Mock()
    task = me_mdl.MesosTaskConfig(
        name='fake_name',
        cpus=20.0,
        mem=2048.0,
        disk=2000.0,
    )

    ef.task_queue.put(task)
    tasks_to_launch = ef.get_tasks_to_launch(fake_offer)

    assert len(tasks_to_launch) == 0
    assert ef.task_queue.qsize() == 1
    assert mock_get_metric.call_count == 1
    assert mock_get_metric.call_args == mock.call(
        ef_mdl.TASK_INSUFFICIENT_OFFER_COUNT
    )
    assert mock_get_metric.return_value.count.call_count == 1
    assert mock_get_metric.return_value.count.call_args == mock.call(1)


def test_create_new_docker_task(
    ef,
    fake_offer,
    fake_task
):
    available_ports = [31200]
    task_id = fake_task.task_id
    task_metadata = ef_mdl.TaskMetadata(
        task_config=fake_task,
        task_state='fake_state',
        task_state_ts=time.time()
    )
    fake_task = fake_task.set(volumes=v(
        Dict(
            mode='RO',
            container_path='fake_container_path',
            host_path='fake_host_path'
        )))

    ef.task_metadata = ef.task_metadata.set(task_id, task_metadata)
    docker_task = ef.create_new_docker_task(
        fake_offer,
        fake_task,
        available_ports
    )

    new_docker_task = Dict(
        task_id=Dict(value=task_id),
        agent_id=Dict(value='fake_agent_id'),
        name='executor-{id}'.format(id=task_id),
        resources=[
            Dict(name='cpus',
                 type='SCALAR',
                 role='fake_role',
                 scalar=Dict(value=10.0)),
            Dict(name='mem',
                 type='SCALAR',
                 role='fake_role',
                 scalar=Dict(value=1024.0)),
            Dict(name='disk',
                 type='SCALAR',
                 role='fake_role',
                 scalar=Dict(value=1000.0)),
            Dict(name='ports',
                 type='RANGES',
                 role='fake_role',
                 ranges=Dict(
                     range=[Dict(begin=31200, end=31200)]))
        ],
        command=Dict(
            value='echo "fake"',
            uris=[]
        ),
        container=Dict(
            type='DOCKER',
            docker=Dict(image='fake_image',
                        network='BRIDGE',
                        force_pull_image=True,
                        port_mappings=[Dict(host_port=31200,
                                            container_port=8888)]),
            parameters=[],
            volumes=[Dict(
                container_path='fake_container_path',
                host_path='fake_host_path',
                mode='RO'
            )]
        )
    )
    assert ef.task_metadata[task_id].agent_id == 'fake_agent_id'
    assert docker_task == new_docker_task


def test_stop(ef):
    ef.stop()

    assert ef.stopping


def test_initialize_metrics(ef):
    default_dimensions = {
        'framework_name': 'fake_name',
        'framework_role': 'fake_role'
    }
    ef_mdl.create_counter = mock.Mock()
    ef_mdl.create_timer = mock.Mock()

    ef._initialize_metrics()

    assert ef_mdl.create_counter.call_count == 10
    ef_mdl_counters = [
        ef_mdl.TASK_LAUNCHED_COUNT,
        ef_mdl.TASK_FINISHED_COUNT,
        ef_mdl.TASK_FAILED_COUNT,
        ef_mdl.TASK_KILLED_COUNT,
        ef_mdl.TASK_LOST_COUNT,
        ef_mdl.TASK_ERROR_COUNT,
        ef_mdl.TASK_ENQUEUED_COUNT,
        ef_mdl.TASK_INSUFFICIENT_OFFER_COUNT,
        ef_mdl.TASK_STUCK_COUNT,
        ef_mdl.BLACKLISTED_AGENTS_COUNT,
    ]
    for cnt in ef_mdl_counters:
        ef_mdl.create_counter.assert_any_call(cnt, default_dimensions)
    assert ef_mdl.create_timer.call_count == 2
    ef_mdl_timers = [
        ef_mdl.TASK_QUEUED_TIME_TIMER,
        ef_mdl.OFFER_DELAY_TIMER
    ]
    for tmr in ef_mdl_timers:
        ef_mdl.create_timer.assert_any_call(tmr, default_dimensions)


def test_slave_lost(ef, fake_driver):
    ef.slaveLost(fake_driver, 'fake_slave_id')


def test_registered(ef, fake_driver):
    ef.registered(
        fake_driver,
        Dict(value='fake_framework_id'),
        'fake_master_info'
    )

    assert ef.driver == fake_driver


def test_reregistered(ef, fake_driver):
    ef.reregistered(
        fake_driver,
        'fake_master_info'
    )


def test_resource_offers_launch(
    ef,
    fake_task,
    fake_offer,
    fake_driver,
    mock_get_metric,
    mock_time


):
    ef.driver = fake_driver
    ef._last_offer_time = 1.0
    mock_time.return_value = 2.0
    ef.suppress_after = 0.0
    ef.offer_matches_pool = mock.Mock(return_value=True)
    task_id = fake_task.task_id
    docker_task = Dict(task_id=Dict(value=task_id))
    task_metadata = ef_mdl.TaskMetadata(
        task_config=fake_task,
        task_state='fake_state',
        task_state_ts=time.time()
    )
    ef.get_tasks_to_launch = mock.Mock(return_value=[docker_task])

    ef.task_queue.put(fake_task)
    ef.task_metadata = ef.task_metadata.set(task_id, task_metadata)
    ef.resourceOffers(ef.driver, [fake_offer])

    assert fake_driver.suppressOffers.call_count == 0
    assert not ef.are_offers_suppressed
    assert fake_driver.declineOffer.call_count == 0
    assert fake_driver.launchTasks.call_count == 1
    assert mock_get_metric.call_count == 2
    mock_get_metric.assert_any_call(ef_mdl.OFFER_DELAY_TIMER)
    mock_get_metric.assert_any_call(ef_mdl.TASK_LAUNCHED_COUNT)
    assert mock_get_metric.return_value.record.call_count == 1
    assert mock_get_metric.return_value.record.call_args == mock.call(1.0)
    assert mock_get_metric.return_value.count.call_count == 1
    assert mock_get_metric.return_value.count.call_args == mock.call(1)


def test_resource_offers_no_tasks_to_launch(
    ef,
    fake_offer,
    fake_driver,
    mock_get_metric
):
    ef.suppress_after = 0.0

    ef.resourceOffers(fake_driver, [fake_offer])

    assert fake_driver.declineOffer.call_args == mock.call(
        fake_offer.id,
        ef.offer_decline_filter
    )
    assert fake_driver.suppressOffers.call_count == 1
    assert ef.are_offers_suppressed
    assert fake_driver.launchTasks.call_count == 0
    assert mock_get_metric.call_count == 0
    assert mock_get_metric.return_value.count.call_count == 0


def test_resource_offers_blacklisted_offer(
    ef,
    fake_task,
    fake_offer,
    fake_driver,
    mock_get_metric
):
    ef.blacklisted_slaves = ef.blacklisted_slaves.append(
        fake_offer.agent_id.value,
    )
    ef.task_queue.put(fake_task)
    ef.resourceOffers(fake_driver, [fake_offer])

    assert fake_driver.declineOffer.call_count == 1
    assert fake_driver.declineOffer.call_args == mock.call(
        fake_offer.id,
        ef.offer_decline_filter
    )
    assert fake_driver.launchTasks.call_count == 0
    assert mock_get_metric.call_count == 0
    assert mock_get_metric.return_value.count.call_count == 0


def test_resource_offers_not_for_pool(
    ef,
    fake_task,
    fake_offer,
    fake_driver,
    mock_get_metric
):
    ef.offer_matches_pool = mock.Mock(return_value=False)

    ef.task_queue.put(fake_task)
    ef.resourceOffers(fake_driver, [fake_offer])

    assert ef.offer_matches_pool.call_count == 1
    assert ef.offer_matches_pool.call_args == mock.call(fake_offer)
    assert fake_driver.declineOffer.call_count == 1
    assert fake_driver.declineOffer.call_args == mock.call(
        fake_offer.id,
        ef.offer_decline_filter
    )
    assert fake_driver.launchTasks.call_count == 0
    assert mock_get_metric.call_count == 0
    assert mock_get_metric.return_value.count.call_count == 0


def test_resource_offers_unmet_reqs(
    ef,
    fake_task,
    fake_offer,
    fake_driver,
    mock_get_metric
):
    ef.get_tasks_to_launch = mock.Mock(return_value=[])

    ef.task_queue.put(fake_task)
    ef.resourceOffers(fake_driver, [fake_offer])

    assert fake_driver.declineOffer.call_count == 1
    assert fake_driver.declineOffer.call_args == mock.call(
        fake_offer.id,
        ef.offer_decline_filter
    )
    assert fake_driver.launchTasks.call_count == 0
    assert mock_get_metric.call_count == 0
    assert mock_get_metric.return_value.count.call_count == 0


def status_update_test_prep(retries, state):
    task = me_mdl.MesosTaskConfig(name='fake_name')
    task_id = task.task_id
    update = Dict(
        task_id=Dict(value=task_id),
        state=state
    )
    task_metadata = ef_mdl.TaskMetadata(
        task_config=task,
        task_state=state,
        task_state_ts=time.time(),
        retries=retries
    )

    return update, task_id, task_metadata


def test_status_update_record_only(
    ef,
    fake_driver
):
    update, task_id, task_metadata = status_update_test_prep(
        0,
        'fake_state1'
    )
    task_metadata = task_metadata.set(task_state='fake_state2')
    ef.translator = mock.Mock()

    ef.task_metadata = ef.task_metadata.set(task_id, task_metadata)
    ef.statusUpdate(fake_driver, update)

    assert ef.task_metadata[task_id].task_state == 'fake_state1'
    assert fake_driver.acknowledgeStatusUpdate.call_count == 1
    assert fake_driver.acknowledgeStatusUpdate.call_args == mock.call(update)


def test_status_update_finished(
    ef,
    fake_driver,
    mock_get_metric
):
    # finished task does same thing as other states
    update, task_id, task_metadata = status_update_test_prep(
        0,
        'TASK_FINISHED'
    )
    ef.translator = mock.Mock()

    ef.task_metadata = ef.task_metadata.set(task_id, task_metadata)
    ef.statusUpdate(fake_driver, update)

    assert task_id not in ef.task_metadata
    assert mock_get_metric.call_count == 1
    assert mock_get_metric.call_args == mock.call(ef_mdl.TASK_FINISHED_COUNT)
    assert mock_get_metric.return_value.count.call_count == 1
    assert mock_get_metric.return_value.count.call_args == mock.call(1)
    assert fake_driver.acknowledgeStatusUpdate.call_count == 1
    assert fake_driver.acknowledgeStatusUpdate.call_args == mock.call(update)


def test_duplicate_status_update(
    ef,
    fake_driver,
    mock_get_metric
):
    update, task_id, task_metadata = status_update_test_prep(
        0,
        'TASK_FINISHED'
    )
    ef.translator = mock.Mock()

    ef.statusUpdate(fake_driver, update)

    assert task_id not in ef.task_metadata
    assert mock_get_metric.call_count == 0
    assert mock_get_metric.return_value.count.call_count == 0
    assert fake_driver.acknowledgeStatusUpdate.call_count == 1
