# Local SQS-compatible ingestion

The official boto3 client sends job UUID notifications to a local ElasticMQ
server. PostgreSQL owns the source manifest, current permissions, attempts,
lease and completion. A message cannot supply a document path or permission.
The worker deletes a message only after durable terminal state. Duplicate
notifications do not republish pages. A failed delete leaves a completed job
available for safe notification redelivery.

Install the optional SDK, preserving retrieval dependencies if you use that lab:

```powershell
uv sync --locked --extra queue --extra retrieval
docker run -d --rm --name creditlens-sqs-check --memory 256m --cpus 1 --pids-limit 128 --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=32m --tmpfs /data:rw,noexec,nosuid,size=32m,uid=1000,gid=1000 -p 127.0.0.1:19324:9324 --mount type=bind,source=C:/Users/chinm/Documents/projects/credit_lens/infra/sqs/elasticmq.conf,target=/opt/elasticmq.conf,readonly softwaremill/elasticmq-native@sha256:e4580ab9ad1bd5cd37b4ba04911bc5ccc8cd2d9ab4de56ece65acee71c24e05c
$env:CREDITLENS_TEST_SQS_ENDPOINT='http://127.0.0.1:19324'
.venv\Scripts\python.exe infra/sqs/wait_ready.py
```

Adjust the absolute config mount path for your checkout. The pinned image is
ElasticMQ 1.7.1. It stores this fixture's queues in memory; stopping the container
discards them. The adapter accepts only numeric loopback HTTP and synthetic
CreditLens queue URLs on that origin. It supplies fixed test credentials and an
explicit endpoint, disables proxies and ignores configured endpoint overrides.
It does not load AWS credentials to perform these operations or contact AWS.

Start the [PostgreSQL fixture](../postgres/README.md), set its test URL, and set
`CREDITLENS_TEST_PARSER_IMAGE` to the built local parser image ID. Then run:

```powershell
.venv\Scripts\pytest.exe infra/postgres/test_queue_execution.py
```

Tests create uniquely named queues and dead-letter queues, configure redrive,
exercise real send/receive/visibility/deletion, and remove only their own queues.
They cover duplicate publication, missing notifications, active leases, unknown
and OCR jobs, malformed-message dead lettering, deletion failure after commit,
circuit recovery and separate operator processes. SDK response injection covers
malformed broker fields and is labeled separately from actual-server evidence.

For operator use, create a local standard queue through the SDK and supply its
returned URL. This Python setup creates a queue and dead-letter policy locally;
the adapter itself does not create queues:

```python
import json
from creditlens.sqs_queue import local_client

client = local_client("http://127.0.0.1:19324")
dead = client.create_queue(QueueName="creditlens-local-dead")["QueueUrl"]
arn = client.get_queue_attributes(QueueUrl=dead, AttributeNames=["QueueArn"])["Attributes"][
    "QueueArn"
]
queue = client.create_queue(
    QueueName="creditlens-local",
    Attributes={"RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 10})},
)["QueueUrl"]
print(queue)
client.close()
```
Use the database/subject/manifest setup in the PostgreSQL guide, then add the two
global options before the command:

```powershell
.venv\Scripts\python.exe scripts/ingest_documents.py --source-root C:/creditlens-sources --queue-endpoint http://127.0.0.1:19324 --queue-url http://127.0.0.1:19324/000000000000/creditlens-local submit --pdf C:/incoming/document.pdf --manifest C:/incoming/manifest.json --subject your-private-admin-subject --key document-v1
.venv\Scripts\python.exe scripts/ingest_documents.py --source-root C:/creditlens-sources --queue-endpoint http://127.0.0.1:19324 --queue-url http://127.0.0.1:19324/000000000000/creditlens-local work-queue --image $env:CREDITLENS_TEST_PARSER_IMAGE --wait 10
```

Submission returns durable job status and `notification: SENT` or `PENDING`.
Failed notification sending leaves the SQL job committed. An empty broker poll
recovers one eligible SQL job. `work-one` remains available for direct SQL recovery.
To connect both API and worker to the same local queue, configure:

```powershell
$env:CREDITLENS_INGESTION_SQS_ENDPOINT='http://127.0.0.1:19324'
$env:CREDITLENS_INGESTION_SQS_QUEUE_URL='http://127.0.0.1:19324/000000000000/creditlens-local'
.venv\Scripts\python.exe scripts/ingest_documents.py --source-root C:/creditlens-sources work-loop --image $env:CREDITLENS_TEST_PARSER_IMAGE --stop-file C:/creditlens-sources/STOP
```

The PostgreSQL/ingestion settings in the other guide must still be enabled.
The CLI uses these queue settings when explicit arguments are absent. The opt-in
API commits a job, returns 202 and sends its notification as a FastAPI background
task. Failed sending logs only a curated code. A crash can lose a background task;
the database job remains recoverable. Readiness depends on the authoritative
database, not the optional notification path.

`work-loop` retains clients and circuit state and processes one bounded job at a
time. It waits one second between iterations by default and polls SQL when the
broker is empty or unavailable. Invalid broker responses produce a curated
failure event rather than a SQL claim. `--interval` accepts 0.1..30 seconds;
`--max-iterations` optionally bounds a verification run to 1..10000 iterations.
It emits JSON lifecycle and outcome events without source text or receipt handles.

Create the configured stop file, or send SIGINT/SIGTERM, to stop after the current
iteration. A stop received during a broker poll leaves its message unacknowledged
and prevents a new claim. A current parser can finish bounded execution and
publication checks. A preexisting stop file causes an immediate stop; use a fresh
path for a later run. The loop is not an installed OS service or crash-restart
supervisor. Transport limits and the current parser bound shutdown latency;
stopping is not always immediate.

Receive visibility is 180 seconds, longer than the digital parser's 120-second
limit plus cleanup. An active lease or delayed retry defers the message 30 seconds;
the database still decides when it can be claimed. Invalid or unknown work stays
unacknowledged for the configured dead-letter policy. Three consecutive failed
broker calls open a 30-second process-local circuit; the next successful probe
resets it. SDK calls use two total attempts, a two-second connect timeout and a
25-second read timeout. Those limits are not an end-to-end request deadline.

ElasticMQ implements a subset of SQS. These checks establish local integration,
not AWS IAM, encryption, availability, persistence or managed deployment. AWS
use remains disabled under the no-spending constraint. The live HF demo is
unchanged and has no public administrative queue access.

The manual CI workflow starts the same pinned fixture and includes these modules
in its core and critical statement-coverage gates. No hosted run is implied by
passing local tests. Stop only the owned fixture when finished:

```powershell
docker stop creditlens-sqs-check
```

Contracts: [ElasticMQ](https://github.com/softwaremill/elasticmq),
[SQS receipt deletion](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_DeleteMessage.html),
[visibility](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ChangeMessageVisibility.html),
[boto3 SQS](https://docs.aws.amazon.com/boto3/latest/reference/services/sqs.html).
