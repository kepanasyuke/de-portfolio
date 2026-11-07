FROM apache/airflow:2.9.3-python3.11

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless procps curl \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

USER airflow

RUN pip install --no-cache-dir \
    apache-airflow-providers-postgres==5.11.5 \
    pyspark==3.5.1 \
    "psycopg[binary]==3.1.19" \
    pyyaml==6.0.1 \
    boto3==1.34.131 \
    prometheus-client==0.20.0

# pip installs console scripts into ~/.local/bin; spark-submit must be on PATH for the DAGs.
ENV PATH="/home/airflow/.local/bin:${PATH}"
