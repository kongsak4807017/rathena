from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from tools.observability.rathena_observer import (
    ProcessSample,
    Snapshot,
    count_established_tcp,
    load_config,
    render_prometheus,
)


class ObserverTests(unittest.TestCase):
    def test_load_config_parses_targets_ports_and_disabled_mysql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "observer.json"
            path.write_text(
                json.dumps(
                    {
                        "listen_port": 9999,
                        "process_targets": [{"name": "map-server", "command_contains": "map-server"}],
                        "tcp_ports": [5121],
                        "mysql_probe": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.listen_port, 9999)
        self.assertEqual(config.tcp_ports, (5121,))
        self.assertEqual(config.process_targets[0].name, "map-server")
        self.assertIsNone(config.mysql_probe_command)

    def test_count_established_tcp_counts_only_established_wanted_ports(self) -> None:
        content = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode
   0: 0100007F:1401 0100007F:C001 01 00000000:00000000 00:00000000 00000000 1000 0 1
   1: 0100007F:1401 0100007F:C002 0A 00000000:00000000 00:00000000 00000000 1000 0 2
   2: 0100007F:1AF4 0100007F:C003 01 00000000:00000000 00:00000000 00000000 1000 0 3
"""
        with tempfile.TemporaryDirectory() as directory:
            tcp = pathlib.Path(directory) / "tcp"
            tcp.write_text(content, encoding="utf-8")
            counts = count_established_tcp([tcp], [5121, 6900])
        self.assertEqual(counts, {5121: 1, 6900: 1})

    def test_render_prometheus_includes_process_tcp_mysql_and_errors(self) -> None:
        snapshot = Snapshot(
            collected_at_seconds=1000.0,
            process_samples=[
                ProcessSample(
                    name="map-server",
                    pid=42,
                    cpu_seconds=12.5,
                    resident_bytes=4096,
                    virtual_bytes=8192,
                    threads=3,
                    open_fds=11,
                    start_time_seconds=50.0,
                )
            ],
            tcp_established={5121: 7},
            mysql_probe_success=True,
            mysql_probe_duration_seconds=0.012,
            collection_errors={"process_not_found:char-server": 1},
        )
        output = render_prometheus(snapshot)
        self.assertIn('rathena_process_up{process="map-server"} 1', output)
        self.assertIn('rathena_process_up{process="char-server"} 0', output)
        self.assertIn('rathena_process_resident_memory_bytes{pid="42",process="map-server"} 4096', output)
        self.assertIn('rathena_tcp_established_connections{port="5121"} 7', output)
        self.assertIn("rathena_mysql_probe_success 1", output)
        self.assertIn('rathena_observer_collection_errors{reason="process_not_found:char-server"} 1', output)


if __name__ == "__main__":
    unittest.main()
