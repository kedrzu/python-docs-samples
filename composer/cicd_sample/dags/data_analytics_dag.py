# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import datetime

from airflow import models
from airflow.providers.google.cloud.operators import dataproc
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import (
    GCSToBigQueryOperator,
)
from airflow.utils.task_group import TaskGroup

PROJECT_NAME = "{{var.value.gcp_project}}"

# BigQuery configs
BQ_DESTINATION_DATASET_NAME = "holiday_weather_michal"
BQ_DESTINATION_TABLE_NAME = "holidays_weather_michal_joined"
BQ_NORMALIZED_TABLE_NAME = "holidays_weather_michal_normalized"


PYSPARK_JAR = "gs://spark-lib/bigquery/spark-bigquery-latest_2.12.jar"
PROCESSING_PYTHON_FILE = "gs://{{var.value.gcs_bucket}}/data_analytics_process.py"

BATCH_ID = "test-batch-id-{{ ts_nodash | lower}}"  # Dataproc serverless only allows lowercase characters
BATCH_CONFIG = {
    "pyspark_batch": {
        "jar_file_uris": [PYSPARK_JAR],
        "main_python_file_uri": PROCESSING_PYTHON_FILE,
        "args": [
            PROJECT_NAME,
            f"{BQ_DESTINATION_DATASET_NAME}.{BQ_DESTINATION_TABLE_NAME}",
            f"{BQ_DESTINATION_DATASET_NAME}.{BQ_NORMALIZED_TABLE_NAME}",
        ],
    },
}

yesterday = datetime.datetime.combine(
    datetime.datetime.today() - datetime.timedelta(1), datetime.datetime.min.time()
)

default_dag_args = {
    # Setting start date as yesterday starts the DAG immediately when it is
    # detected in the Cloud Storage bucket.
    "start_date": yesterday,
    # To email on failure or retry set 'email' arg to your email and enable
    # emailing here.
    "email_on_failure": False,
    "email_on_retry": False,
    # If a task fails, retry it once after waiting at least 5 minutes
    "retries": 1,
    "retry_delay": datetime.timedelta(minutes=5),
}

with models.DAG(
    "summit_dag",
    # Continue to run DAG once per day
    schedule_interval=datetime.timedelta(days=1),
    default_args=default_dag_args,
) as dag:

    create_batch = dataproc.DataprocCreateBatchOperator(
        task_id="create_batch",
        project_id=PROJECT_NAME,
        region="{{ var.value.gce_region }}",
        batch=BATCH_CONFIG,
        batch_id=BATCH_ID,
    )
    load_external_dataset = GCSToBigQueryOperator(
        task_id="run_bq_external_ingestion",
        bucket="{{var.value.gcs_bucket}}",
        source_objects=["holidays.csv"],
        destination_project_dataset_table=f"{BQ_DESTINATION_DATASET_NAME}.holidays",
        source_format="CSV",
        schema_fields=[
            {"name": "Date", "type": "DATE"},
            {"name": "Holiday", "type": "STRING"},
        ],
        skip_leading_rows=1,
    )

    with TaskGroup("join_bq_datasets") as bq_join_group:

        for year in range(1997, 2022):
            # BigQuery configs
            BQ_DATASET_NAME = f"bigquery-public-data.ghcn_d.ghcnd_{str(year)}"
            BQ_DESTINATION_TABLE_NAME = "holidays_weather_joined"
            # Specifically query a Chicago weather station
            WEATHER_HOLIDAYS_JOIN_QUERY = f"""
            SELECT Holidays.Date, Holiday, id, element, value
            FROM `{PROJECT_NAME}.holiday_weather.holidays` AS Holidays
            JOIN (SELECT id, date, element, value FROM {BQ_DATASET_NAME} AS Table WHERE Table.element="TMAX" AND Table.id="USW00094846") AS Weather
            ON Holidays.Date = Weather.Date;
            """

            bq_join_holidays_weather_data = BigQueryInsertJobOperator(
                task_id=f"bq_join_holidays_weather_data_{str(year)}",
                configuration={
                    "query": {
                        "query": WEATHER_HOLIDAYS_JOIN_QUERY,
                        "useLegacySql": False,
                        "destinationTable": {
                            "projectId": PROJECT_NAME,
                            "datasetId": BQ_DESTINATION_DATASET_NAME,
                            "tableId": BQ_DESTINATION_TABLE_NAME,
                        },
                        "writeDisposition": "WRITE_APPEND",
                    }
                },
                location="US",
            )

        load_external_dataset >> bq_join_group >> create_batch
