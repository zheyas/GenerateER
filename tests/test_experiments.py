"""Смоук-тест опытов из tools/experiments.py: опыты запускаются, а главные инварианты выполняются."""
from common import Counter

from tools import experiments as E

T = Counter()
check = T.check

r = E.mutation_experiment()
rows = r["rows"]
check("опыт 4: на чистых схемах нет нарушений и подозрений", r["clean"]["errors"] == 0 and r["clean"]["warnings"] == 0, r["clean"])
check("опыт 4: безобидные столбцы с разными именами не вызывают замечаний", r["benign"]["errors"] == 0 and r["benign"]["warnings"] == 0, r["benign"])
check("опыт 4: одинаковые имена у родителя и потомка — только подозрения", r["benign_shared_names"]["errors"] == 0 and r["benign_shared_names"]["warnings"] > 0, r["benign_shared_names"])
check("опыт 4: при записанных зависимостях находятся все дефекты и форма верна на любых названиях",
      all(x["found"] == x["n"] and x["level_ok"] == x["n"] for x in rows if x["fds"]), [x for x in rows if x["fds"] and x["found"] != x["n"]])
check("опыт 4: привычные названия находятся без зависимостей, а непривычные — нет",
      all(x["found"] == x["n"] for x in rows if x["naming"] == "conv" and not x["fds"] and x["kind"] not in ("partial_fd", "bcnf_fd", "hierarchy"))
      and all(x["found"] == 0 for x in rows if x["naming"] == "unconv" and not x["fds"]))
check("опыт 4: дефекты, требующие зависимостей, без них не находятся", all(x["found"] == 0 for x in rows if x["kind"] in ("partial_fd", "bcnf_fd") and not x["fds"]))
check("опыт 6а: круг SQL без расхождений", E.roundtrip_sql(40)["mismatches"] == 0)
res = E.roundtrip_text(40)
check("опыт 6б: круг короткой записи без расхождений", res["mismatches"] == 0 and res["relations"] > 0, res)
t = E.trigger_experiment()
atk = t["attacks"]
check("опыт 7: все слепые нагрузки и нагрузки на схему SQLite отклонены", atk["слепые"]["blocked"] == atk["слепые"]["payloads"] and atk["схема SQLite"]["blocked"] == atk["схема SQLite"]["payloads"], atk)
py = t["benign"]["Python (pydoc_data.topics)"]
check("опыт 7: ложных срабатываний на посторонней прозе меньше 1 %", py["lines"] > 1000 and py["blocked"] / py["lines"] < 0.01, py["blocked"] / max(1, py["lines"]))
check("опыт 7: большинство исходных нагрузок отклоняется", sum(a["blocked"] for a in atk.values()) / sum(a["payloads"] for a in atk.values()) > 0.7)
T.finish()
