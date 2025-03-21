#!/usr/bin/env python3

import argparse
import logging
import os
import requests
import schedule
import signal
import time
from datetime import datetime
from influxdb_client import InfluxDBClient, Point, BucketRetentionRules
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.domain.write_precision import WritePrecision
from itertools import zip_longest
from urllib.parse import urljoin, urlencode, urlparse, urlunparse


DEFAULT_INTERVAL_SECONDS = 60

DEFAULT_PIHOLE_ALIAS = 'pihole'
DEFAULT_PIHOLE_ADDRESS = 'http://pi.hole:80'
DEFAULT_PIHOLE_TOKEN = None

DEFAULT_PIHOLE_NUM_TOP_ITEMS = 10
DEFAULT_PIHOLE_NUM_TOP_CLIENTS = 10

DEFAULT_INFLUXDB_ADDRESS = 'http://influxdb:8086'
DEFAULT_INFLUXDB_ORG = 'my-org'
DEFAULT_INFLUXDB_TOKEN = None
DEFAULT_INFLUXDB_BUCKET = 'pihole'
DEFAULT_INFLUXDB_CREATE_BUCKET = False
DEFAULT_INFLUXDB_VERIFY_SSL = True

DEBUG = False

'''
Class to contain a Pi-hole configuration.
'''
class Pihole():

    def __init__(self, alias, address, token=None):
        self.alias = alias
        self.address = address
        self.token = token

'''
Class to contain the application configuration.
'''
class Config():

    def __init__(self, args):
        # Set configuration by first checking for command-line arguments. If not present, check for the corresponding
        # environment variable. If not present, use the default.
        self.interval_seconds = int(args.interval or os.getenv("INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
        pihole_aliases = (args.pihole_alias or os.getenv("PIHOLE_ALIAS", DEFAULT_PIHOLE_ALIAS)).split(',')
        pihole_addresses = (args.pihole_address or os.getenv("PIHOLE_ADDRESS", DEFAULT_PIHOLE_ADDRESS)).split(',')
        if len(pihole_addresses) == 0:
            logging.error("No Pi-hole instances provided")
            exit(1)
        if len(pihole_aliases) != len(pihole_addresses):
            logging.error('The number of Pi-hole aliases provided does not match the number of Pi-hole addresses')
            exit(1)
        pihole_tokens = (args.pihole_token or os.getenv("PIHOLE_TOKEN", DEFAULT_PIHOLE_TOKEN))
        pihole_tokens = pihole_tokens.split(',') if pihole_tokens else list()
        self.piholes = dict()
        for alias,address,token in zip_longest(pihole_aliases, pihole_addresses, pihole_tokens):
            if address in self.piholes:
                logging.warning(f'Duplicate Pi-hole address provided ({address}), skipping...')
                continue
            if not token:
                logging.warning(f'No auth token provided for {alias}, some data will not be available')
            self.piholes[address] = Pihole(alias, address, token)
        self.num_top_items = int(args.pihole_num_top_items or os.getenv("PIHOLE_NUM_TOP_ITEMS", DEFAULT_PIHOLE_NUM_TOP_ITEMS))
        self.num_top_clients = int(args.pihole_num_top_clients or os.getenv("PIHOLE_NUM_TOP_CLIENTS", DEFAULT_PIHOLE_NUM_TOP_CLIENTS))
        self.influxdb_address = args.influxdb_address or os.getenv("INFLUXDB_ADDRESS", DEFAULT_INFLUXDB_ADDRESS)
        self.influxdb_org = args.influxdb_org or os.getenv("INFLUXDB_ORG", DEFAULT_INFLUXDB_ORG)
        self.influxdb_token = args.influxdb_token or os.getenv("INFLUXDB_TOKEN", DEFAULT_INFLUXDB_TOKEN)
        if not self.influxdb_token:
            logging.error('No InfluxDB auth token provided')
            exit(1)
        self.influxdb_bucket = args.influxdb_bucket or os.getenv("INFLUXDB_BUCKET", DEFAULT_INFLUXDB_BUCKET)
        self.influxdb_create_bucket = args.influxdb_create_bucket or os.getenv("INFLUXDB_CREATE_BUCKET", DEFAULT_INFLUXDB_CREATE_BUCKET)
        self.influxdb_verify_ssl = bool(args.influxdb_skip_verify_ssl if args.influxdb_skip_verify_ssl is not None else os.getenv("INFLUXDB_VERIFY_SSL", DEFAULT_INFLUXDB_VERIFY_SSL))

    '''
    Dump the configuration to the log.
    '''
    def dump(self):
        logging.info('================== Configuration ==================')
        logging.info(f'Pi-holes:            {list(self.piholes.values())[0].alias} {list(self.piholes.values())[0].address} ' + ('(Auth token provided)' if list(self.piholes.values())[0].token else '(No auth token)'))
        for pihole in list(self.piholes.values())[1:]:
            logging.info(f'                     {pihole.alias} {pihole.address} ' + ('(Auth token provided)' if pihole.token else '(No auth token)'))
        logging.info(f'Poll interval:       {self.interval_seconds} seconds')
        logging.info(f'InfluxDB address:    {self.influxdb_address}')
        logging.info(f'InfluxDB org:        {self.influxdb_org}')
        logging.info(f'InfluxDB token:      {"******" if self.influxdb_token else "(None)"}')
        logging.info(f'InfluxDB bucket:     {self.influxdb_bucket}')
        logging.info(f'InfluxDB verify SSL: {self.influxdb_verify_ssl}')
        logging.info('===================================================')
        return

class PiholeInfluxDB():

    def __init__(self, config):
        self.config = config

    '''
    Ensure that the target InfluxDB bucket exists, creating it if necessary.
    '''
    def _verify_bucket(self):
        influxdb_client = InfluxDBClient(url=self.config.influxdb_address, token=self.config.influxdb_token, org=self.config.influxdb_org, verify_ssl=self.config.influxdb_verify_ssl)
        try:
            buckets_api = influxdb_client.buckets_api()
            if buckets_api.find_bucket_by_name(self.config.influxdb_bucket) is None:
                if self.config.influxdb_create_bucket:
                    logging.info(f'InfluxDB bucket does not yet exist - creating...')
                    retention_rules = BucketRetentionRules(type="expire", every_seconds=604800) # 7-day retention
                    buckets_api.create_bucket(bucket_name=self.config.influxdb_bucket, org=self.config.influxdb_org, retention_rules=retention_rules)
                else:
                    logging.error(f'InfluxDB bucket does not exist')
                    return False
        except Exception as e:
            logging.error(f'Error creating InfluxDB bucket: {str(e)}')
            return False
        return True

    '''
    Execute a GET request against the Pi-hole API.
    '''
    def _pihole_api_get(self, pihole, query=None, auth_token=None):
        url = self._get_pihole_api_url(pihole, query, auth_token)
        try:
            # Set the request timeout to be either 50% of the polling interval, or 30 seconds, whichever is smaller.
            response = requests.get(url, timeout=min(0.5 * self.config.interval_seconds, 30))
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            logging.error(f'[{pihole.alias}] [HTTP {response.status_code}] Error executing request to {url}: {e}')
        except requests.exceptions.ConnectionError as e:
            logging.error(f'[{pihole.alias}] Error connecting to {pihole.address}: {e}')
        except requests.exceptions.Timeout as e:
            logging.error(f'[{pihole.alias}] Timeout connecting to {pihole.address}: {e}')
        except requests.exceptions.RequestException as e:
            logging.error(f'[{pihole.alias}] Unexpected error while sending request to {url}: {e}')
        return None

    '''
    Get the URL for the Pi-hole API with an optional query element.
    '''
    def _get_pihole_api_url(self, pihole, query=None, auth_token=None):
        url = f'{pihole.address}/admin/api.php'
        query_data = list()
        if query:
            query_data.append(query)
        if auth_token:
            query_data.append(f'auth={auth_token}')
        if len(query_data) > 0:
            url = f'{url}?{"&".join(query_data)}'
        return url

    '''
    Gets general Pi-hole statistics for the instance.
    '''
    def _get_stats(self, pihole):
        queries = ["summaryRaw", f'topItems={self.config.num_top_items}', f'topClients={self.config.num_top_clients}', "getForwardDestinations", "getQueryTypes"]
        response = self._pihole_api_get(pihole, "&".join(queries), pihole.token)
        if response:
            logging.debug(response.json())
            return response.json()
        return None

    '''
    Gets the number of blocked and total domains in 10 minute intervals over the past 24 hours for the instance.
    '''
    def _get_10min_data(self, pihole):
        response = self._pihole_api_get(pihole, "overTimeData10mins", pihole.token)
        if response:
            logging.debug(response.json())
            data = response.json()
            if data == []:
                logging.warning('No data in response')
                return None, None
            return dict(data['domains_over_time']), dict(data['ads_over_time'])
        return None, None

    '''
    Write all data gathered to InfluxDB.
    '''
    def _write_to_influxdb(self, pihole, stats, domains_over_time, ads_over_time):
        now_seconds = int(time.time())
        hostname = urlparse(pihole.address).hostname
        tags = {
            "alias": pihole.alias,
            "hostname": hostname
        }
        points=[]

        # Gravity
        gravity = stats.pop("gravity_last_updated")
        points.append(Point.from_dict(
            {
                "measurement": "gravity",
                "tags": tags,
                "fields": {
                    "file_exists": gravity['file_exists'],
                    "last_updated": gravity['absolute'],
                    "seconds_since_update": (gravity['relative']['days'] * 86400) + (gravity['relative']['hours'] * 3600) + (gravity['relative']['minutes'] * 60)
                },
                "time": now_seconds
            },
            WritePrecision.S
        ))

        # Replies
        replies = {
            'UNKNOWN': stats.pop("reply_UNKNOWN"),
            'NODATA': stats.pop("reply_NODATA"),
            'NXDOMAIN': stats.pop("reply_NXDOMAIN"),
            'CNAME': stats.pop("reply_CNAME"),
            'IP': stats.pop("reply_IP"),
            'DOMAIN': stats.pop("reply_DOMAIN"),
            'RRNAME': stats.pop("reply_RRNAME"),
            'SERVFAIL': stats.pop("reply_SERVFAIL"),
            'REFUSED': stats.pop("reply_REFUSED"),
            'NOTIMP': stats.pop("reply_NOTIMP"),
            'OTHER': stats.pop("reply_OTHER"),
            'DNSSEC': stats.pop("reply_DNSSEC"),
            'NONE': stats.pop("reply_NONE"),
            'BLOB': stats.pop("reply_BLOB")
        }
        points.append(Point.from_dict(
            {
                "measurement": "replies",
                "tags": tags,
                "fields": replies,
                "time": now_seconds
            },
            WritePrecision.S
        ))

        # Check for stats that required authentication
        if 'top_queries' in stats:
            points.append(Point.from_dict(
                {
                    "measurement": "top_queries",
                    "tags": tags,
                    "fields": {
                        "top_10": self._json_to_csv(stats.pop('top_queries'))
                    },
                    "time": now_seconds
                },
                WritePrecision.S
            ))
        if 'top_ads' in stats:
            points.append(Point.from_dict(
                {
                    "measurement": "top_ads",
                    "tags": tags,
                    "fields": {
                        "top_10": self._json_to_csv(stats.pop('top_ads'))   
                    },
                    "time": now_seconds
                },
                WritePrecision.S
            ))
        if 'top_sources' in stats:
            points.append(Point.from_dict(
                {
                    "measurement": "top_sources",
                    "tags": tags,
                    "fields": {
                        "top_10": self._json_to_csv(stats.pop('top_sources'))   
                    },
                    "time": now_seconds
                },
                WritePrecision.S
            ))
        if 'forward_destinations' in stats:
            forward_destinations = stats.pop('forward_destinations')
            points.append(Point.from_dict(
                {
                    "measurement": "forward_destinations",
                    "tags": tags,
                    "fields": forward_destinations,
                    "time": now_seconds
                },
                WritePrecision.S,
                field_types={x: "float" for x in forward_destinations}
            ))
        if 'querytypes' in stats:
            querytypes = stats.pop('querytypes')
            points.append(Point.from_dict(
                {
                    "measurement": "query_types",
                    "tags": tags,
                    "fields": querytypes,
                    "time": now_seconds
                },
                WritePrecision.S,
                field_types={x: "float" for x in querytypes}
            ))

        # Remaining stats
        stats['ads_percentage_today'] = float(stats['ads_percentage_today']) # Ensure this is always a float, even when 0
        stats['status'] = 1 if stats['status'] == "enabled" else 0
        points.append(Point.from_dict(
            {
                "measurement": "stats",
                "tags": tags,
                "fields": stats,
                "time": now_seconds
            },
            WritePrecision.S
        ))

        # Domains over time
        for timestamp,count in domains_over_time.items():
            points.append(Point.from_dict(
                {
                    "measurement": "over_time_data",
                    "tags": tags,
                    "fields": {
                        "domains_over_time": count
                    },
                    "time": int(timestamp)
                },
                WritePrecision.S
            ))
        
        # Ads over time
        for timestamp,count in ads_over_time.items():
            points.append(Point.from_dict(
                {
                    "measurement": "over_time_data",
                    "tags": tags,
                    "fields": {
                        "ads_over_time": count
                    },
                    "time": int(timestamp)
                },
                WritePrecision.S
            ))
        
        # Batch write of points
        influxdb_client = InfluxDBClient(url=self.config.influxdb_address, token=self.config.influxdb_token, org=self.config.influxdb_org, verify_ssl=self.config.influxdb_verify_ssl)
        try:
            with influxdb_client.write_api(write_options=SYNCHRONOUS) as write_api:
                write_api.write(self.config.influxdb_bucket, self.config.influxdb_org, record=points)
        except Exception as e:
            logging.error(f'Error writing data to InfluxDB: {str(e)}')
            return False
        return True

    '''
    Utility function to take a JSON object and convert the fields to a comma-separated list of key-value pairs.
    For example: {'example.com': 123, 'google.com': 456}
    Would become: "example.com:123,google.com:456"

    Table data (such as Top Queries, Top Ads, etc.) are stored this way to facilitate grouping and querying only the latest set of domains, ads, etc.
    It's simpler to store the data as CSV, then apply transformations in a tool such as Grafana for display purposes.
    '''
    def _json_to_csv(self, data):
        return ','.join([f'{key}:{value}' for key, value in dict(data).items()])

    '''
    Runs the scheduled polling job for a single Pi-hole.
    '''
    def _run_job(self, pihole):
        query_start = datetime.now()
        stats = self._get_stats(pihole)
        domains_over_time, ads_over_time = self._get_10min_data(pihole)
        query_end = datetime.now()
        if stats and domains_over_time and ads_over_time:
            logging.info(f'[{pihole.alias}] Queried successfully in {int((query_end - query_start).total_seconds() * 1000)}ms')
            write_start = datetime.now()
            if self._write_to_influxdb(pihole, stats, domains_over_time, ads_over_time):
                write_end = datetime.now()
                logging.info(f'[{pihole.alias}] Wrote to InfluxDB successfully in {int((write_end - write_start).total_seconds() * 1000)}ms')
        return

    '''
    Starts the scheduled polling jobs for each Pi-hole instance.
    '''
    def start(self):
        logging.info('Starting...')
        # Ensure the target bucket exists
        if not self._verify_bucket():
            exit(1)
        # Schedule one job per Pi-hole instance to monitor
        for pihole in self.config.piholes.values():
            job = schedule.every(self.config.interval_seconds).seconds.do(self._run_job, pihole=pihole)
            job.run() # Run immediately without initial delay
        # Run until stopped
        while True:
            schedule.run_pending()
            time.sleep(1)

'''
Handler for SIGTERM and SIGINT signals.
'''
def signal_handler(signum, frame):
    if signum in [signal.SIGTERM, signal.SIGINT]:
        logging.info('Stopping...')
        exit(0)
    exit(1)

def main():
    # Parse any command-line arguments, which take a higher precedence than environment variables
    parser = argparse.ArgumentParser(description='Query Pi-hole instances for statistics and store them in InfluxDB')
    parser.add_argument('-i', '--interval',
        type=int,
        help=f'interval (in seconds) between queries to the Pi-hole instance(s) (Default: {DEFAULT_INTERVAL_SECONDS})')
    parser.add_argument('--pihole-alias',
        type=str,
        help=f'comma-separated list of aliases for Pi-hole instances (Default: {DEFAULT_PIHOLE_ALIAS})')
    parser.add_argument('--pihole-address',
        type=str,
        help=f'comma-separated list of addresses for Pi-hole instances (Default: {DEFAULT_PIHOLE_ADDRESS})')
    parser.add_argument('--pihole-token',
        type=str,
        help=f'comma-separated list of Pi-hole API tokens (Default: {DEFAULT_PIHOLE_TOKEN})')
    parser.add_argument('--pihole-num-top-items',
        type=int,
        help=f'number of top domains queried and ad domains (Default: {DEFAULT_PIHOLE_NUM_TOP_ITEMS})')
    parser.add_argument('--pihole-num-top-clients',
        type=int,
        help=f'number of top clients (Default: {DEFAULT_PIHOLE_NUM_TOP_CLIENTS})')
    parser.add_argument('--influxdb-address',
        type=str,
        help=f'address of the InfluxDB server (Default: {DEFAULT_INFLUXDB_ADDRESS})')
    parser.add_argument('--influxdb-org',
        type=str,
        help=f'InfluxDB organization to use (Default: {DEFAULT_INFLUXDB_ORG})')
    parser.add_argument('--influxdb-bucket',
        type=str,
        help=f'InfluxDB bucket to store data (Default: {DEFAULT_INFLUXDB_BUCKET})')
    parser.add_argument('--influxdb-create-bucket',
        action='store_true',
        help=f'Create the InfluxDB bucket if it does not already exist')
    parser.add_argument('--influxdb-token',
        type=str,
        help=f'InfluxDB auth token (Default: {DEFAULT_INFLUXDB_TOKEN})')
    parser.add_argument('--influxdb-skip-verify-ssl',
        action='store_false',
        help=f'Skip verification of the SSL certificate for InfluxDB')
    parser.add_argument('-d', '--debug',
        action='store_true',
        help=f'Enable debug logging (Default: {DEBUG})')
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.debug or os.getenv("DEBUG") else logging.INFO
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=log_level, datefmt="%H:%M:%S")
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    config = Config(args)
    config.dump()
    # TODO: Log application version
    PiholeInfluxDB(config).start()

if __name__ == "__main__":
    main()