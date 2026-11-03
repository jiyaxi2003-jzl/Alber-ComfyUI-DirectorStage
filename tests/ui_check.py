"""在真实 ComfyUI 页面里验证导演台前端：扩展是否注册、按钮是否挂上、能不能取到场景。

用法：
    python tests/ui_check.py --base http://127.0.0.1:8188 --out shots
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8188")
    ap.add_argument("--out", default="shots")
    ap.add_argument("--keep", action="store_true", help="结束后不关浏览器")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    proc = cdp.launch()
    pg = cdp.Page()
    ok = True
    try:
        pg.new_target("about:blank")
        pg.call("Page.enable")
        pg.call("Runtime.enable")
        pg.call("Log.enable")
        pg.call("Page.navigate", url=args.base)
        # 等前端 boot：app 与 graph 都就绪才算数（扩展多的时候要十几秒）
        ready = False
        for _ in range(24):
            pg.pump(2)
            ready = bool(pg.eval("!!(window.app && window.app.graph && window.LiteGraph)"))
            if ready:
                break
        pg.pump(3)  # 留点时间给扩展注册表落定

        # --- 1. 前端基本可用
        print(f"[1] 前端 boot: {'OK' if ready else 'FAIL'}")
        ok &= bool(ready)

        # --- 2. 扩展是否注册
        ext = pg.eval(
            "(async()=>{const api=window.comfyAPI?.api?.api;"
            "const list=await (api?api.getExtensions?.():fetch('/extensions').then(r=>r.json()));"
            "return (list||[]).filter(x=>x.includes('DirectorStage')).length;})()",
            await_promise=True,
        )
        print(f"[2] /extensions 命中导演台文件数: {ext}")

        loaded = pg.eval("JSON.stringify(window.__director3dStage || null)")
        print(f"[2] 扩展是否执行: {loaded}")
        ok &= loaded not in (None, "null")

        # --- 3. 建节点，看按钮
        info = pg.eval(
            """(()=>{
              const n = LiteGraph.createNode("DirectorStage");
              if (!n) return JSON.stringify({error:"createNode 返回空，节点没注册上"});
              app.graph.add(n);
              n.pos = [80, 120];
              app.canvas.setDirty(true, true);
              return JSON.stringify({
                widgets: (n.widgets||[]).map(w=>({name:w.name,type:w.type})),
                hasState: !!(n.widgets||[]).find(w=>w.name==="state"),
              });
            })()"""
        )
        print(f"[3] 节点控件: {info}")
        ok &= "打开 Alber-3D 导演台" in (info or "")

        # --- 4. 取场景接口（没跑过上游时应是 404 + 中文提示）
        scene_res = pg.eval(
            "(async()=>{const r=await fetch('/director3d/scene');"
            "let b=null;try{b=await r.json()}catch(e){}"
            "return JSON.stringify({status:r.status,body:b});})()",
            await_promise=True,
        )
        print(f"[4] /director3d/scene -> {scene_res}")

        # --- 5. 点按钮，看它是否走通（没场景时应弹出错误 toast 而不是静默）
        clicked = pg.eval(
            """(()=>{
              const n = app.graph._nodes.find(x=>x.type==="DirectorStage");
              const b = (n.widgets||[]).find(w=>w.name==="打开 Alber-3D 导演台");
              if (!b) return "no-button";
              try { b.callback(); return "called"; } catch(e) { return "throw:"+e.message; }
            })()"""
        )
        print(f"[5] 点按钮: {clicked}")
        pg.pump(3)
        pg.shot(os.path.join(args.out, "ui_stage.png"))

        # --- 6. 控制台里的报错
        bad = [e for e in pg.errors if "director" in e.lower() or "director3d" in e.lower()]
        bad += [c for c in pg.console if "director" in c.lower()]
        print(f"[6] 页面异常 {len(pg.errors)} 条，其中与导演台相关 {len(bad)} 条")
        for b in bad[:10]:
            print("    !", b)
        ok &= not bad

    finally:
        pg.ws.close()
        if not args.keep:
            proc.terminate()

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
