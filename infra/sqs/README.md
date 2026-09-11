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
returned URL. Give it a deliberate dead-letter policy and retention configuration;
the test suite demonstrates the API calls. The adapter does not create queues.
Use the database/subject/manifest setup in the PostgreSQL guide, then add the two
global options before the command:

```powershell
.venv\Scripts\python.exe scripts/ingest_documents.py --source-root C:/creditlens-sources --queue-endpoint http://127.0.0.1:19324 --queue-url http://127.0.0.1:19324/000000000000/creditlens-local submit --pdf C:/incoming/document.pdf --manifest C:/incoming/manifest.json --subject your-private-admin-subject --key document-v1
.venv\Scripts\python.exe scripts/ingest_documents.py --source-root C:/creditlens-sources --queue-endpoint http://127.0.0.1:19324 --queue-url http://127.0.0.1:19324/000000000000/creditlens-local work-queue --image $env:CREDITLENS_TEST_PARSER_IMAGE --wait 10
```

Submission returns durable job status and `notification: SENT` or `PENDING`.
Failed notification sending leaves the SQL job committed. An empty broker poll
recovers one eligible SQL job. During a broker outage, `work-one` without queue
options remains available for direct SQL recovery. Each invocation processes at
most one job; a long-running scheduler is not implemented. Admin HTTP submissions
currently register SQL intent and rely on this polling recovery rather than
sending notifications directly.

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
