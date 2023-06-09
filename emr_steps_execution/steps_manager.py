import boto3
import time
import argparse
import os
import logging
from typing import Iterable, Iterator
from functools import wraps

logging.basicConfig(format='%(asctime)s: %(levelname)s: %(module)s: %(message)s', level=logging.INFO)


def polling_intervals(
    start: Iterable[float], rest: float, max_duration: float = None
) -> Iterator[float]:
    def _intervals():
        yield from start
        while True:
            yield rest

    cumulative = 0.0
    for interval in _intervals():
        cumulative += interval
        if max_duration is not None and cumulative > max_duration:
            break
        yield interval


def check_if_step_is_running(client, cluster_id, job_name):
    active_steps = client.list_steps(ClusterId=cluster_id, StepStates=['PENDING', 'CANCEL_PENDING', 'RUNNING'])['Steps']

    for s in active_steps:
        if s['Name'] == job_name:
            logging.info('There is already active Spark %s job.', job_name)
            return s['Id']

    return None


def get_step_status(client, cluster_id, step_id):
    return client.list_steps(ClusterId=cluster_id, StepIds=[step_id])['Steps'][0]['Status']


def add_emr_step(client, cluster_id, bucket, job_type, executor_memory, executor_cores, driver_memory,
                 driver_cores, executor_number, local, trigger_once, db_url, db_pass):

    script_path = os.path.join(bucket, 'emr-submit-spark.sh')
    python_modules_path = [
                            os.path.join(bucket, 'utils.zip'),
                          ]

    spark_app_path = os.path.join(bucket, f'{job_type}.py')
    spark_app_args = f'--config config.yaml'

    if job_type == 'batch':
        spark_app_args += f' --db-url {db_url}  -db_pass {db_pass}'
    elif job_type == 'streaming':
        if trigger_once:
            spark_app_args += " --trigger_once"
    else:
        raise RuntimeError(f'unknown job type: {job_type}')

    spark_job_params = f"""
        --master yarn \
        --deploy-mode client \
        --executor-memory {executor_memory} \
        --executor-cores {executor_cores} \
        --driver-memory {driver_memory} \
        --driver-cores {driver_cores} \
        --num-executors {executor_number} \
        --py-files {','.join(python_modules_path)} \
        --conf maximizeResourceAllocation=true \
        {spark_app_path} \
        {spark_app_args}
    """

    response = client.add_job_flow_steps(
        JobFlowId=cluster_id,
        Steps=[
            {
                'Name': job_type,
                'ActionOnFailure': 'CONTINUE',
                'HadoopJarStep': {
                    'Jar': 's3://us-east-1.elasticmapreduce/libs/script-runner/script-runner.jar',
                    'Args': [
                        script_path,
                        spark_job_params
                    ]
                }
            },
        ]
    )

    if not response['ResponseMetadata']['HTTPStatusCode'] == 200:
        print(response)
        raise RuntimeError(f'Failed to instantiate {job_type} step')

    return response['StepIds'][0]


def wait_for_step_to_finish(client, cluster_id, step_id):
    intervals = polling_intervals([2.0, 4.0, 8.0], 10.0)

    while True:
        status = get_step_status(client, cluster_id, step_id)

        if status['State'] not in ['PENDING', 'CANCEL_PENDING', 'RUNNING']:
            return status

        time.sleep(next(intervals))


def execute_steps(cluster_id, job_type, bucket,
                  executor_memory, executor_cores, driver_memory,
                  driver_cores, executor_number, scale, local):

    emr_client = boto3.client('emr')

    logging.info('Check whether there is no active Spark %s job', job_type)
    step_id = check_if_step_is_running(emr_client, cluster_id, job_type)

    if step_id is None:
        logging.info('Executing new Spark %s job', job_type)
        step_id = add_emr_step(
            client=emr_client,
            cluster_id=cluster_id,
            bucket=bucket,
            job_type=job_type,
            executor_memory=executor_memory,
            executor_cores=executor_cores,
            driver_memory=driver_memory,
            driver_cores=driver_cores,
            executor_number=executor_number,
            scale=scale,
            local=local
        )
    else:
        logging.info(
            'Active Spark %s job found, id: %s. ' 'Subscribing to its logs',
            job_type, step_id
        )

    status = wait_for_step_to_finish(emr_client, cluster_id, step_id)

    if status['State'] == 'COMPLETED':
        logging.info('%s job succeeded', job_type)
        return

    raise RuntimeError((
        f"""Something wrong with Spark {job_type} job.
        Job was finished with status: {status['State']}"""
    ))


def retry(max_consecutive_errors=3, delay=30):
    """
    Retry calling the decorated function which collect executed batches state
    Args:
        max_consecutive_errors: Number of times to try (not retry) before
                                giving up.
        delay: Initial delay between retries in seconds.
    """

    def retry_decorator(f):
        @wraps(f)
        def steps_restart(*args, **kwargs):
            remaining_errors = max_consecutive_errors

            while True:
                try:
                    f(**kwargs)
                    remaining_errors = max_consecutive_errors
                except Exception as e:
                    msg = f'{e}. Retrying in {delay} seconds...'
                    logging.warning(msg)
                    time.sleep(delay)
                    remaining_errors -= 1

                if (remaining_errors < 1 and max_consecutive_errors > 0) or remaining_errors < 0:
                    raise Exception("No errors remaining, failed to start new Spark job")
                elif remaining_errors < 1:
                    break

        return steps_restart
    return retry_decorator


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='operational_client_status',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument('--emr_cluster_id', required=True)
    parser.add_argument('--s3_bucket', required=True)
    parser.add_argument('--job_type', type=str, choices=['batch', 'streaming'])
    parser.add_argument('--local', type=bool, default=False)
    parser.add_argument('--executor_memory', required=True)
    parser.add_argument('--executor_cores', required=True)
    parser.add_argument('--driver_memory', required=True)
    parser.add_argument('--driver_cores', type=int, default=1)
    parser.add_argument('--executor_number', type=int, default=1)
    parser.add_argument('--trigger_once', type=bool, default=False)
    parser.add_argument('--max_consecutive_errors', type=int, default=3)
    parser.add_argument('--batch_restart_delay', type=int, default=30)
    parser.add_argument('--db-url', type=str, default='localhost')
    parser.add_argument('--db-pass', type=str)

    args = parser.parse_args()

    if args.trigger_once:
        max_consecutive_errors = 0
    else:
        max_consecutive_errors = args.max_consecutive_errors

    retry_decorator = retry(max_consecutive_errors=max_consecutive_errors,
                            delay=args.batch_restart_delay)
    run_spark_jobs = retry_decorator(execute_steps)

    run_spark_jobs(
        cluster_id=args.emr_cluster_id,
        bucket=f's3://{args.s3_bucket}',
        job_type=args.job_type,
        executor_memory=args.executor_memory,
        executor_cores=args.executor_cores,
        driver_memory=args.driver_memory,
        driver_cores=args.driver_cores,
        executor_number=args.executor_number,
        local=args.local
    )
