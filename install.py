# installer for weewx-grafana
# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)

from weecfg.extension import ExtensionInstaller

VERSION = "0.1.0"


def loader():
    return GrafanaInstaller()


class GrafanaInstaller(ExtensionInstaller):
    def __init__(self):
        super(GrafanaInstaller, self).__init__(
            version=VERSION,
            name='grafana',
            description='Upload weather data to Grafana Cloud using OTLP.',
            author="",
            author_email="",
            restful_services='user.grafana.GrafanaCloud',
            config={
                'StdRESTful': {
                    'GrafanaCloud': {
                        'otlp_endpoint': 'INSERT_OTLP_ENDPOINT_HERE',
                        'instance_id': 'INSERT_INSTANCE_ID_HERE',
                        'api_key': 'INSERT_API_KEY_HERE',
                    }
                }
            },
            files=[('bin/user', ['bin/user/grafana.py',
                                  'bin/user/grafana_metrics.py',
                                  'bin/user/grafana_backfill.py'])]
            )