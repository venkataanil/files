import tempfile
import csv
import io
import boto3
import uuid
import os

s3bucket = "kube-burner-ai-s3-bucket"
CHUNK_SIZE=10

def split_dict_into_chunks(d, chunk_size):
    it = iter(d.items())

    while True:
        chunk = {}

        for _ in range(chunk_size):
            try:
                key, value = next(it)
                chunk[key] = value
            except StopIteration:
                break

        if not chunk:
            break

        yield chunk


def upload_csv_to_s3(data, bucket, filename):
    # use temporary file
    tmp = tempfile.NamedTemporaryFile(mode='w+', newline='', delete=False)
    try:
        writer = csv.writer(tmp)
        writer.writerow(["Metric", "Value"])
        for chunk in split_dict_into_chunks(data, CHUNK_SIZE):
            for k, v in chunk.items():
                writer.writerow([k, v])
        tmp.flush()

        # Initialise AWS s3 client from existing AWS config
        s3 = boto3.client("s3")
        s3.upload_file(tmp.name, bucket, filename)
        print(f"Uploaded to s3://{bucket}/{filename}")

    finally:
        tmp.close()
        os.remove(tmp.name)
        print(f"Temporary file {tmp.name} deleted")

large_dict = {f"key{i}": f"value{i}" for i in range(100)}
upload_csv_to_s3(large_dict, s3bucket, str(uuid.uuid1()))
