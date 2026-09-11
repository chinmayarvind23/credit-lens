"""Wait for the owned local broker before dependent integration tests, without cloud calls."""

import os
import time

from botocore.exceptions import BotoCoreError, ClientError

from creditlens.sqs_queue import local_client


def main() -> None:
    """Retry for 20 seconds; SDK transport limits also bound the final in-flight attempt."""
    client = local_client(os.environ["CREDITLENS_TEST_SQS_ENDPOINT"])
    deadline = time.monotonic() + 20
    try:
        while True:
            try:
                client.list_queues(MaxResults=1)
                print("Local SQS-compatible fixture is ready")
                return
            except (BotoCoreError, ClientError):
                if time.monotonic() >= deadline:
                    raise SystemExit("Local SQS-compatible fixture did not become ready") from None
                time.sleep(0.25)
    finally:
        client.close()


if __name__ == "__main__":
    main()
