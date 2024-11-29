# X-Road Subsystems Methods and Services Catalogue Collector

This application will collect methods and services for the X-Road subsystems catalogue. The Collector will output the collected data to a directory that is ready to be served by a web server (like Apache or Nginx). Subsequent executions will create new versions of the catalogue, preserving older versions.

Application provides "fs" plugin for storing data in local filesystem and "minio" plugin to store data in MinIO (S3). It is also possible to create other custom storage plugins.

## Configuration

Create a configuration file for your X-Road instance using an example configuration file: [example-config.yaml](example-config.yaml). If you need to provide catalogue data for multiple X-Road instances then you will need separate configurations for each X-Road instance.

Common configuration parameters:
* `storage_plugin` - Plugin to use for data storage ("fs", "minio" or custom plugin);
* `server_url` - Security Server URL used by collector;
* `client` - Array of X-Road client identifiers used for X-Road queries;
* `instance` - X-Road instance to collect data from;
* `timeout` - Collector queries timeout;
* `server_cert` - Optional TLS certificate or CA certificate of your Security Server for verification;
* `client_cert` - Optional application TLS certificate for authentication with security server;
* `client_key` - Optional application key for authentication with security server;
* `thread_count` - Amount of parallel threads to use;
* `wsdl_replaces` - Replace metadata like creation timestamp in WSDLs to avoid duplicates;
* `excluded_member_codes` - Exclude certain members who are permanently in faulty state or should not be queried for any other reasons;
* `excluded_subsystem_codes` - Exclude certain members who are permanently in faulty state or should not be queried for any other reasons;
* `logging-config` - logging configuration passed to logging.config.dictConfig(). You can read more about Python3 logging here: [https://docs.python.org/3/library/logging.config.html](https://docs.python.org/3/library/logging.config.html).

Filesystem plugin ("fs") configuration parameters:
* `output_path` - Filesystem path to use for catalogue storage.

MinIO plugin ("minio") configuration parameters:
* `minio_url` - Address of your MinIO server;
* `minio_access_key` - Access key for MinIO;
* `minio_secret_key` - Secret key for MinIO;
* `minio_secure` - Boolean flag indicating if secure HTTPS connection is used for MinIO;
* `minio_ca_certs` - CA certificate for validating MinIO certificate;
* `minio_bucket` - MinIO bucket used for file storage;
* `minio_path` - Path inside MinIO bucket used for file storage.

Configuration parameter common for "fs" and "minio" plugins:
* `filtered_hours` - Include "filtered_hours" or first catalogue version of every hour;
* `filtered_days` - Include "filtered_days" or first catalogue version of every day;
* `filtered_months` - Include "filtered_months" or first catalogue version of every month;
* `cleanup_interval` - Interval in days when automatic removal of older catalogue versions will be performed. During the cleanup only the first report of each day is preserved and extra reports are deleted;
* `days_to_keep` - amount of latest days to protect against cleanup.

## Installing python venv

Python virtual environment is an easy way to manage application dependencies. First You will need to install support for python venv:
```bash
sudo apt install python3-venv
```

Then install collector and required python modules into venv:
```bash
python3 -m venv venv
source venv/bin/activate
# Install xrdinfo module
pip install xrdinfo_module/
# Without MinIO support
pip install .
# Or with MinIO support
pip install .[minio]
```

## Running

You can run the collector by issuing command (with activated venv):
```bash
python -m xrd_collector config-instance1.json
# Or alternatively
xrd-collector config-instance1.json
```

## Systemd timer

Systemd timer can be used as more advanced version of cron. You can use provided example timer and service definitions to perform scheduled collection of data from your instances.

Add service description `systemd/catalogue-collector.service` to `/lib/systemd/system/catalogue-collector.service` and timer description `systemd/catalogue-collector.timer` to `/lib/systemd/system/catalogue-collector.timer`.

Then start and enable automatic startup:
```bash
sudo systemctl daemon-reload
sudo systemctl start catalogue-collector.timer
sudo systemctl enable catalogue-collector.timer
```

## Developing

To test minio locally on linux machine execute the following commands (note that you should never use the default password for production):
```bash
sudo mkdir -p local/minio
docker run -d -p 9000:9000 --name minio1 -e "MINIO_ACCESS_KEY=minioadmin" -e "MINIO_SECRET_KEY=minioadmin" -v $(pwd)/local/minio:/data minio/minio server /data
cd
wget https://dl.min.io/client/mc/release/linux-amd64/mc
chmod +x mc
~/mc config host add --quiet --api s3v4 cat http://localhost:9000 minioadmin minioadmin
~/mc mb cat/catalogue
~/mc anonymous set download cat/catalogue
```

To copy catalogue data to minio execute the following command:
```bash
~/mc cp -r EE cat/catalogue/
```

In order create new storage plugins it is required to create "xrd_collector.plugin" entrypoint and implement new plugin based on xrd_collector.storage.PluginBase.
