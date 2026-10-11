"""The Freerouting command line, settings round trip and Java version parsing."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from autorouter.engine.freerouting import (RELEASE, RouteOptions, build_command, dsn_net_class_name,
                                           find_jar, managed_jar_path)
from autorouter.engine.java import parse_java_version
from autorouter.engine.paths import data_dir


def main():
    os.environ["KICAD_AUTOROUTER_HOME"] = tempfile.mkdtemp()
    assert data_dir() == Path(os.environ["KICAD_AUTOROUTER_HOME"])

    # Defaults produce a minimal, headless command.
    cmd = build_command("/usr/bin/java", Path("/x/fr.jar"), RouteOptions(), Path("/w/b.dsn"),
                        Path("/w/b.ses"), host_version="0.1.0", user_data=Path("/w/data"))
    assert cmd[:2] == ["/usr/bin/java", "-Djava.awt.headless=true"]
    assert "-jar" in cmd and cmd[cmd.index("-jar") + 1] == "/x/fr.jar"
    assert cmd[cmd.index("-de") + 1] == "/w/b.dsn" and cmd[cmd.index("-do") + 1] == "/w/b.ses"
    assert "--gui.enabled=false" in cmd and "-da" in cmd
    assert cmd[cmd.index("-host") + 1] == "KiCad-Autorouter 0.1.0"
    assert "--user_data_path=/w/data" in cmd
    assert "--router.autorouter.max_passes=100" in cmd
    assert "--router.optimizer.improvement_threshold=2.5" in cmd
    assert "--router.via_costs=50" in cmd
    for flag in ("-mt", "-us", "-is", "-hr", "-inc", "-Xmx"):
        assert not any(a == flag or a.startswith(flag) for a in cmd), (flag, cmd)
    assert not any(a.startswith("--router.layers") for a in cmd)
    assert not any(a.startswith("--router.fanout") for a in cmd)

    # Everything set.
    o = RouteOptions(max_passes=20, optimizer_threads=4, improvement_threshold=1.0, via_costs=80,
                     fanout=False, update_strategy="hybrid", hybrid_ratio="2:1",
                     selection_strategy="random", ignore_net_classes=["Default", "Power"],
                     routable_layers=[True, False, True], horizontal_layers=[True, False, True],
                     max_heap_mb=4096, extra_args="--router.job_timeout=00:30:00 -l en")
    cmd = build_command("java", Path("fr.jar"), o, Path("b.dsn"), Path("b.ses"))
    assert "-Xmx4096m" in cmd
    assert cmd[cmd.index("-mt") + 1] == "4"
    assert "--router.autorouter.max_passes=20" in cmd
    assert "--router.optimizer.improvement_threshold=1" in cmd
    assert "--router.fanout.enabled=false" in cmd
    assert cmd[cmd.index("-us") + 1] == "hybrid" and cmd[cmd.index("-hr") + 1] == "2:1"
    assert cmd[cmd.index("-is") + 1] == "random"
    assert cmd[cmd.index("-inc") + 1] == "kicad_default,Power"
    assert "--router.layers.routable=true,false,true" in cmd
    assert "--router.layers.preferred_direction_horizontal=true,false,true" in cmd
    assert cmd[-3:] == ["--router.job_timeout=00:30:00", "-l", "en"]

    # -1 threads disables the optimizer; all-routable sends nothing.
    cmd = build_command("java", Path("fr.jar"), RouteOptions(optimizer_threads=-1,
                        routable_layers=[True, True]), Path("b.dsn"), Path("b.ses"))
    assert cmd[cmd.index("-mt") + 1] == "0"
    assert not any(a.startswith("--router.layers") for a in cmd)

    assert dsn_net_class_name("Default") == "kicad_default"
    assert dsn_net_class_name("Power") == "Power"

    # Options survive JSON, unknown keys are ignored, missing keys default.
    data = json.loads(json.dumps(o.to_dict()))
    data["bogus"] = 1
    del data["via_costs"]
    back = RouteOptions.from_dict(data)
    assert back.max_passes == 20 and back.via_costs == 50 and back.routable_layers == [True, False, True]

    # Jar lookup: explicit path wins when it exists, else the managed download.
    assert find_jar("/nonexistent/x.jar") is None
    managed = managed_jar_path()
    assert managed == data_dir() / "freerouting" / RELEASE.jar_name
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"")
    assert find_jar() == managed
    assert find_jar("/nonexistent/x.jar") == managed

    # `java -version` output of several vendors and ages.
    assert parse_java_version('openjdk version "27" 2026-09-15') == 27
    assert parse_java_version('openjdk version "21.0.12.1" 2026-08-18\nOpenJDK Runtime') == 21
    assert parse_java_version('java version "1.8.0_292"') == 8
    assert parse_java_version('openjdk version "25.0.4.1" 2026-07-21 LTS') == 25
    assert parse_java_version("Unable to locate a Java Runtime.") is None

    # Settings round trip through the data dir.
    from autorouter.settings import Settings
    s = Settings(java_path="/j", jar_path="", options=o)
    s.save()
    loaded = Settings.load()
    assert loaded.java_path == "/j" and loaded.options.ignore_net_classes == ["Default", "Power"]
    print("test_command: ok")


if __name__ == "__main__":
    main()
