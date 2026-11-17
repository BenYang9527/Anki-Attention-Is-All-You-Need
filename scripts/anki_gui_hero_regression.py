#!/usr/bin/env python3
"""Hero/concentration regressions: layout voids, click-through set, dark mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from anki.collection import Collection
from aqt.profiles import ProfileManager


ROOT = Path(__file__).parents[1]
PROFILE = "Dashboard Hero QA"
HELPER = r'''
from __future__ import annotations
import json
import os
from pathlib import Path
from aqt import gui_hooks, mw
from aqt.qt import QTimer

OUT = Path(os.environ["ATTENTION_QA_OUTPUT"])
IDS = json.loads(Path(os.environ["ATTENTION_QA_FIXTURE"]).read_text())
result = {}
attempts = {}
finished = False

def finish():
    global finished
    if finished:
        return
    finished = True
    OUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    QTimer.singleShot(0, mw.app.quit)

def page():
    import attention_is_all_you_need as addon
    return addon._window.web.page()

def capture(name):
    folder = os.environ.get('ATTENTION_QA_SCREENSHOTS')
    if folder:
        import attention_is_all_you_need as addon
        addon._window.grab().save(str(Path(folder) / (name + '.png')))

def poll(name, expression, callback, limit=100):
    attempts[name] = attempts.get(name, 0) + 1
    def checked(value):
        if value:
            callback()
        elif attempts[name] < limit:
            QTimer.singleShot(100, lambda: poll(name, expression, callback, limit))
        else:
            result[name + "Timeout"] = True
            finish()
    page().runJavaScript(expression, checked)

def hero_geometry_script():
    return """(()=>{
      const hero=document.querySelector('.hero');
      const actions=document.querySelector('.hero-actions');
      const side=document.querySelector('.hero-side');
      const sideRows=document.querySelectorAll('.hero-side .ledger-row').length;
      if(!hero||!actions||!side){return JSON.stringify({heroPresent:false});}
      const heroRect=hero.getBoundingClientRect();
      const actionsRect=actions.getBoundingClientRect();
      const ctaGap=heroRect.bottom-actionsRect.bottom;
      const style=getComputedStyle(hero);
      const oneLine=parseFloat(style.lineHeight)||21;
      const sideText=side.innerText;
      return JSON.stringify({
        heroPresent:true,
        mode:state.dashboard.hero.mode,
        stateTag:document.querySelector('.state-tag')?.innerText||'',
        stateTagNearTitle:!!document.querySelector('.hero-kicker .state-tag'),
        barsClickable:!!document.querySelector('button.bar-row[data-open-concentration]'),
        ctaLabel:state.dashboard.hero.ctaLabel,
        sideRows,
        sideKeepsTwoRows:sideRows===2&&sideText.includes('复习预算')&&
          sideText.includes('新增空间')&&!sideText.includes('系统状态')&&!sideText.includes('计划新增'),
        newSpaceMatchesPayload:(function(){
          const rows=document.querySelectorAll('.hero-side .ledger-row');
          const value=rows[1]?rows[1].querySelector('b').innerText:'';
          return !!value&&value===state.dashboard.capacity.health.newSpaceValue;
        })(),
        newSpaceValue:state.dashboard.capacity.health.newSpaceValue,
        newSpaceNote:state.dashboard.capacity.health.newSpaceNote,
        weeklyNew:state.dashboard.capacity.health.weeklyNew,
        dailyPace:state.dashboard.capacity.health.dailyPace,
        forecastInfoButton:!!document.querySelector('[data-info="forecast"]'),
        ctaGap:Math.round(ctaGap*10)/10,
        ctaGapWithinSpec:ctaGap>=24&&ctaGap<=42,
        ctaGapUnderOneLine:ctaGap<=oneLine*2,
        heroHeight:Math.round(heroRect.height),
        heroCompact:heroRect.height<=400,
        noMinHeight:['auto','0px'].includes(style.minHeight),
        heroNoPercentiles:!hero.innerText.includes('P50')&&!hero.innerText.includes('P90'),
        newFlowVisible:document.body.innerText.includes('本周生成')
      });
    })()"""

def check_hero_info(callback):
    # “预测说明”：点击展开、内容来自真实预测、再点收起。
    page().runJavaScript("""(()=>{
      const button=document.querySelector('[data-info=forecast]');
      const pop=document.querySelector('[data-info-pop=forecast]');
      if(!button||!pop){return JSON.stringify({forecastInfoOpens:false,forecastInfoCloses:false});}
      button.click();
      const opened=!pop.hidden&&pop.innerText.includes('分钟');
      button.click();
      return JSON.stringify({forecastInfoOpens:opened,forecastInfoCloses:pop.hidden});
    })()""", lambda raw: (result.update(json.loads(raw)), callback())[1])

def settled_shot(name, callback):
    # DOM 更新后 Chromium 合成器仍可能持有旧帧；先等待再抓图，
    # 否则会截到“正在计算真实 Anki 数据…”的加载态。
    def shot():
        capture(name)
        callback()
    QTimer.singleShot(800, shot)

def check_hero(callback):
    def completed(raw):
        result.update(json.loads(raw))
        settled_shot('hero-light', callback)
    page().runJavaScript(hero_geometry_script(), completed)

def check_narrow(callback):
    import attention_is_all_you_need as addon
    addon._window.resize(680, 820)
    def completed(raw):
        result.update(json.loads(raw))
        addon._window.resize(1200, 820)
        callback()
    QTimer.singleShot(500, lambda: page().runJavaScript("""(()=>{
      const hero=document.querySelector('.hero');
      const actions=document.querySelector('.hero-actions');
      const side=document.querySelector('.hero-side');
      if(!hero||!actions||!side){return JSON.stringify({narrowheroPresent:false});}
      const heroRect=hero.getBoundingClientRect();
      const actionsRect=actions.getBoundingClientRect();
      const sideRect=side.getBoundingClientRect();
      const lastRow=side.querySelector('.ledger-row:last-child');
      const stackGap=sideRect.top-actionsRect.bottom;
      const belowSideGap=heroRect.bottom-(lastRow?lastRow.getBoundingClientRect().bottom:sideRect.bottom);
      return JSON.stringify({
        narrowheroPresent:true,
        narrowStackGap:Math.round(stackGap*10)/10,
        narrowBelowSideGap:Math.round(belowSideGap*10)/10,
        narrowStackedImmediately:stackGap>=-2&&stackGap<=44,
        narrowSideFlush:belowSideGap>=14&&belowSideGap<=44,
        narrownoMinHeight:['auto','0px'].includes(getComputedStyle(hero).minHeight),
      });
    })()""", completed))

def open_concentration():
    page().runJavaScript(
        "document.querySelector('button.bar-row[data-open-concentration]').click();true",
        lambda _: poll("heroDetail", "!!state.detail", check_concentration_entry),
    )

def check_concentration_entry():
    def completed(raw):
        result.update(json.loads(raw))
        if not result.get("concentrationEntry"):
            finish()
            return
        settled_shot('concentration-light', check_stats_info)
    page().runJavaScript("""(()=>{
      const summary=document.querySelector('.concentration-summary');
      return JSON.stringify({
        concentrationEntry: state.view==='cards'&&state.queue==='concentration'&&state.filter==='c30'&&state.sort==='avg',
        exactSet: JSON.stringify(currentRows().map(r=>r.cardId))===
          JSON.stringify(state.dashboard.concentration.queue.map(r=>r.cardId)),
        rowCount: currentRows().length,
        slowCount: state.dashboard.concentration.slowCount,
        summarySentence: !!summary&&summary.innerText.includes('这 6 张卡')&&
          summary.innerText.includes('占到期卡片的 42.9%')&&
          summary.innerText.includes('占估算复习耗时的 90%'),
        summaryScopeVisible: !!summary&&summary.innerText.includes('14 张到期且有复习历史的卡片'),
        summaryBarsLabeled: !!summary&&summary.innerText.includes('卡片占比')&&
          summary.innerText.includes('耗时占比'),
        summaryNoPercentiles: !!summary&&!summary.innerText.includes('P50')&&
          !summary.innerText.includes('个人异常线'),
        statsInfoButton: !!document.querySelector('[data-info=concentration-stats]'),
        chipThirtyLabel: [...document.querySelectorAll('[data-filter]')].some(b=>b.innerText.includes('≥30秒 · 6')),
        sortedDesc: currentRows().every((row,index)=>index===0||currentRows()[index-1].avgMs>=row.avgMs),
        avgEvidenceVisible: document.body.innerText.includes('平均耗时')
      });
    })()""", completed)

def check_stats_info(callback=None):
    # “统计说明”：P50/P90 等详细统计收进弹层，点击展开、再点收起。
    page().runJavaScript("""(()=>{
      const button=document.querySelector('[data-info=concentration-stats]');
      const pop=document.querySelector('[data-info-pop=concentration-stats]');
      if(!button||!pop){return JSON.stringify({statsInfoOpens:false,statsInfoCloses:false});}
      button.click();
      const opened=!pop.hidden&&pop.innerText.includes('P50')&&pop.innerText.includes('个人异常线');
      button.click();
      return JSON.stringify({statsInfoOpens:opened,statsInfoCloses:pop.hidden});
    })()""", lambda raw: (result.update(json.loads(raw)), check_filter_linkage())[1])

def check_filter_linkage():
    page().runJavaScript(
        "document.querySelector('[data-filter=c60]').click();true",
        lambda _: poll("chipSixty", "currentRows().length===3", read_chip_sixty),
    )

def read_chip_sixty():
    def completed(raw):
        result.update(json.loads(raw))
        page().runJavaScript(
            "document.querySelector('[data-filter=coutlier]').click();true",
            lambda _: poll("chipOutlier", "currentRows().length===1", read_chip_outlier),
        )
    page().runJavaScript("""(()=>{
      const summary=document.querySelector('.summary-line');
      return JSON.stringify({
        filterLinkSixty: currentRows().length===3&&state.dashboard.concentration.filters.c60.count===3&&
          !!summary&&summary.innerText.includes('这 3 张卡'),
        chipSixtyCount: state.dashboard.concentration.filters.c60.count
      });
    })()""", completed)

def read_chip_outlier():
    def completed(raw):
        result.update(json.loads(raw))
        page().runJavaScript(
            "document.querySelector('[data-filter=c30]').click();true",
            lambda _: poll("chipThirtyBack", "currentRows().length===6", go_dark),
        )
    page().runJavaScript("""(()=>{
      const summary=document.querySelector('.summary-line');
      return JSON.stringify({
        filterLinkOutlier: currentRows().length===1&&state.dashboard.concentration.filters.coutlier.count===1&&
          !!summary&&summary.innerText.includes('这 1 张卡')
      });
    })()""", completed)

def hide_first_card():
    poll("hideReady", "!!state.detail&&!!document.querySelector('[data-hide-open]')", run_hide)

def run_hide():
    page().runJavaScript(
        "document.querySelector('[data-hide-open]').click();"
        "document.querySelector('[data-hide-confirm]').click();true",
        lambda _: poll(
            "afterHide",
            "!!(state.message&&state.message.includes('已隐藏整条笔记'))",
            check_hidden_exit,
            limit=150,
        ),
    )

def check_hidden_exit():
    def completed(raw):
        result.update(json.loads(raw))
        # 列表按平均耗时降序，被隐藏的是首位的 E（平均 110 秒）。
        note = mw.col.get_note(IDS["noteE"])
        result["hiddenTagWritten"] = "看板忽略" in note.tags
        finish()
    page().runJavaScript("""JSON.stringify({
      hideExitsSet: state.dashboard.concentration.slowCount===5&&currentRows().length===5,
      queueAfterHide: state.dashboard.concentration.queue.map(r=>r.cardId)
    })""", completed)

def go_dark():
    result["darkStep"] = "scheduled"
    def with_dark_view():
        result["darkStep"] = "view"
        try:
            capture('concentration-dark')
        except Exception as exc:
            result["captureDarkError"] = str(exc)
        def overview():
            result["darkStep"] = "overview"
            try:
                capture('hero-dark')
            except Exception as exc:
                result["captureDarkError2"] = str(exc)
            def done(raw):
                result["darkStep"] = "done"
                try:
                    result.update(json.loads(raw))
                except Exception as exc:
                    result["darkCheckError"] = str(exc)
                # 截图完成后经导航回到卡片视图，隐藏测试放在最后。
                page().runJavaScript(
                    "document.querySelector('[data-view=cards]').click();true",
                    lambda _: hide_first_card(),
                )
            page().runJavaScript(
                "JSON.stringify({dark:getComputedStyle(document.body).color==='rgb(236, 234, 242)'})",
                done,
            )
        page().runJavaScript("document.querySelector('[data-view=overview]').click();true",
                             lambda _: QTimer.singleShot(800, overview))
    page().runJavaScript(
        "document.documentElement.classList.add('night-mode');true",
        lambda _: QTimer.singleShot(800, with_dark_view),
    )

def start():
    result["step"] = "start"
    import attention_is_all_you_need as addon
    addon.open_dashboard()
    poll("dashboard", "!!state.dashboard",
         lambda: check_hero(lambda: check_hero_info(lambda: check_narrow(open_concentration))))

gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(0, start))
QTimer.singleShot(45000, lambda: (result.update({"watchdogTimeout": True}), finish()))
'''


def create_fixture(base: Path) -> dict[str, int]:
    manager = ProfileManager(base)
    manager.setupMeta()
    manager.meta["firstRun"] = False
    manager.meta["defaultLang"] = "en_US"
    manager.create(PROFILE)
    manager.set_last_loaded_profile_name(PROFILE)
    manager.load(PROFILE)
    manager.save()
    profile_dir = base / PROFILE
    profile_dir.mkdir(parents=True, exist_ok=True)
    col = Collection(str(profile_dir / "collection.anki2"))
    try:
        model = col.models.by_name("Basic")
        ids: dict[str, int] = {}
        slow_keys = ("A", "B", "C", "D", "E", "F")
        fast_keys = tuple(f"G{index}" for index in range(1, 9))
        for key in slow_keys + fast_keys:
            note = col.new_note(model)
            note["Front"] = f"Slow card {key}" if key in slow_keys else f"Fast card {key}"
            note["Back"] = f"Answer {key}"
            col.add_note(note, col.decks.id_for_name("Default"))
            ids["note" + key] = int(note.id)
            ids["card" + key] = int(note.card_ids()[0])
        now = int(time.time() * 1000)
        # 平均耗时分层：≥30 秒 6 张、≥60 秒 3 张、超个人异常线 1 张，
        # 三个筛选集合互不相同，供筛选联动断言使用。
        durations_by_key = {
            "A": (44_000, 46_000),  # 平均 45 秒
            "B": (44_000, 46_000),
            "C": (64_000, 66_000),  # 平均 65 秒
            "D": (64_000, 66_000),
            "E": (109_000, 111_000),  # 平均 110 秒，超异常线（105 秒）
            "F": (30_000, 30_000),  # 平均 30 秒，覆盖筛选边界
        }
        # cards.due 是以集合创建日为基准的天数；is:due 匹配 due <= sched.today。
        due_today = col.sched.today
        for offset, key in enumerate(slow_keys + fast_keys):
            durations = durations_by_key.get(key, (5_000, 5_000))
            for review_index, duration in enumerate(durations):
                col.db.execute(
                    "insert into revlog (id,cid,usn,ease,ivl,lastIvl,factor,time,type) values (?,?,?,?,?,?,?,?,?)",
                    now + offset * 10 + review_index,
                    ids["card" + key],
                    -1,
                    3,
                    1,
                    0,
                    0,
                    duration,
                    1,
                )
            # Flip to the review queue and due today so is:due matches.
            col.db.execute(
                "update cards set queue=2, type=2, due=?, ivl=10, factor=2500, reps=3, lapses=0 where id=?",
                due_today,
                ids["card" + key],
            )
        return ids
    finally:
        col.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="attention-hero-") as folder:
        base = Path(folder)
        fixture = create_fixture(base)
        output = base / "result.json"
        fixture_path = base / "fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        addons = base / "addons21"
        addons.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT / "addon", addons / "attention_is_all_you_need",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "meta.json"))
        helper = addons / "attention_gui_hero_regressions"
        helper.mkdir()
        (helper / "__init__.py").write_text(HELPER, encoding="utf-8")
        code = (
            "import sys,aqt;"
            f"aqt.AnkiApp.KEY={('attention-hero-' + base.name)!r};"
            f"sys.argv=['anki','-b',{str(base)!r},'-p',{PROFILE!r}];"
            "aqt.run()"
        )
        env = dict(os.environ)
        env["ATTENTION_QA_OUTPUT"] = str(output)
        env["ATTENTION_QA_FIXTURE"] = str(fixture_path)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
        screenshots = env.get("ATTENTION_QA_SCREENSHOTS")
        if screenshots:
            Path(screenshots).mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            print((exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            if output.exists():
                print(output.read_text(encoding="utf-8"))
            raise
        if not output.exists():
            print(completed.stdout)
            raise SystemExit("Anki hero regressions did not produce a result")
        result = json.loads(output.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        required = [
            "heroPresent", "mode", "barsClickable", "sideRows",
            "ctaGapWithinSpec", "ctaGapUnderOneLine", "heroCompact", "noMinHeight",
            "heroNoPercentiles", "newFlowVisible", "stateTagNearTitle", "sideKeepsTwoRows",
            "newSpaceMatchesPayload",
            "forecastInfoButton", "forecastInfoOpens", "forecastInfoCloses",
            "narrowheroPresent", "narrowStackedImmediately", "narrowSideFlush", "narrownoMinHeight",
            "concentrationEntry", "exactSet", "rowCount", "slowCount",
            "summarySentence", "summaryScopeVisible", "summaryBarsLabeled", "summaryNoPercentiles",
            "statsInfoButton", "statsInfoOpens", "statsInfoCloses",
            "chipThirtyLabel", "sortedDesc", "avgEvidenceVisible",
            "filterLinkSixty", "filterLinkOutlier",
            "hideExitsSet", "hiddenTagWritten", "dark",
        ]
        numeric = [
            (result.get("rowCount") == 6),
            (result.get("slowCount") == 6),
            (result.get("chipSixtyCount") == 3),
            (result.get("sideRows") == 2),
            (result.get("mode") == "longtail"),
            (result.get("ctaLabel") == "查看高耗时卡片"),
        ]
        passed = (
            completed.returncode == 0
            and all(result.get(key) for key in required)
            and all(numeric)
        )
        if not passed:
            print('\n'.join(line for line in completed.stdout.splitlines() if ':ERROR:' not in line)[-9000:])
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
