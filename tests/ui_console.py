"""在真实 ComfyUI 页面里打开导演台，检查 3D 视口是否真的渲染出东西。

前置：先跑过一次「视频转3D」，让后端缓存里有场景。
用法： python tests/ui_console.py --base http://127.0.0.1:8199 --out shots
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8188")
    ap.add_argument("--out", default="shots")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    proc = cdp.launch()
    pg = cdp.Page()
    ok = True
    try:
        url = args.base.rstrip("/") + "/?dstage_once=1"  # 快照模式：保留 WebGL 帧
        pg.new_target("about:blank")
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            pg.call(m)
        pg.call("Page.navigate", url=url)
        for _ in range(24):
            pg.pump(2)
            if pg.eval("!!(window.app && window.app.graph)"):
                break
        pg.pump(2)

        # 清空默认图，只留导演台节点，方便截图
        pg.eval("(()=>{app.graph.clear();const n=LiteGraph.createNode('DirectorStage');"
                "app.graph.add(n);n.pos=[120,140];app.canvas.ds.scale=1.5;"
                "app.canvas.ds.offset=[-60,-80];app.canvas.setDirty(true,true);return 1;})()")
        pg.pump(1)

        btn = pg.eval(
            "(()=>{const n=app.graph._nodes[0];"
            "const b=(n.widgets||[]).find(w=>w.name==='打开 Alber-3D 导演台');"
            "if(!b) return 'no-button'; b.callback(); return 'clicked';})()"
        )
        print("[1] 点开导演台:", btn)
        ok &= btn == "clicked"

        # 等场景拉取 + three 初始化 + 贴图解码
        appeared = False
        for i in range(20):
            pg.pump(1)
            if pg.eval("!!document.querySelector('.ds-window')"):
                appeared = True
                break
        print("[2] 面板出现:", appeared)
        ok &= appeared
        pg.pump(4)

        # 视口里到底渲染了什么：读 WebGL 帧的统计量
        stats = pg.eval(
            """(()=>{
              const cv = document.querySelector('.ds-viewport canvas');
              if (!cv) return JSON.stringify({error:"视口里没有 canvas"});
              const gl = cv.getContext('webgl2') || cv.getContext('webgl');
              const w = cv.width, h = cv.height;
              const px = new Uint8Array(w*h*4);
              gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,px);
              let sum=0, nonBlack=0, uniq=new Set();
              for(let i=0;i<px.length;i+=4){
                const l=px[i]+px[i+1]+px[i+2];
                sum+=l; if(l>24) nonBlack++;
                if(uniq.size<64) uniq.add(px[i]<<16|px[i+1]<<8|px[i+2]);
              }
              const n=w*h;
              return JSON.stringify({w,h,mean:(sum/n/3).toFixed(1),
                nonBlackPct:(nonBlack/n*100).toFixed(1), colors:uniq.size,
                objs: window.__ds?.scene?.children?.length ?? null});
            })()"""
        )
        print("[3] 视口像素:", stats)
        try:
            s = json.loads(stats or "{}")
            ok &= float(s.get("nonBlackPct", 0)) > 5 and int(s.get("colors", 0)) > 8
        except Exception as exc:
            print("    解析失败:", exc)
            ok = False

        # 对象列表 + 面板文案
        panels = pg.eval(
            "(()=>{const l=document.querySelector('.ds-left');"
            "const items=[...document.querySelectorAll('.ds-left li,.ds-obj')].map(e=>e.textContent.trim());"
            "return JSON.stringify({items:items.slice(0,12), head:document.querySelector('.ds-window')?.innerText?.split('\\n').slice(0,6)});})()"
        )
        print("[4] 左栏:", panels)

        pg.shot(os.path.join(args.out, "console_shot.png"))

        # 解锁到自由环绕，确认分层卡片真的立起来了
        pg.eval("""(()=>{const c=document.querySelector('#ds-lock');
          if(!c) return 0; c.checked=false; c.dispatchEvent(new Event('change')); return 1;})()""")
        pg.pump(3)
        pg.shot(os.path.join(args.out, "console_orbit.png"))

        # 点一下「取当前视角为机位」，验证写入路径
        saved = pg.eval(
            """(()=>{
              const btns=[...document.querySelectorAll('.ds-window button')];
              const t=btns.find(b=>b.textContent.includes('取当前视角'));
              if(!t) return 'no-btn:'+btns.map(b=>b.textContent.trim()).join('|');
              t.click(); return 'clicked';
            })()"""
        )
        print("[5] 取当前视角:", saved)
        pg.pump(1)

        # 保存到节点：state widget 里应该出现可解析的机位 JSON
        sv = pg.eval(
            """(()=>{
              const btns=[...document.querySelectorAll('.ds-window button')];
              const t=btns.find(b=>b.textContent.includes('保存到节点'));
              if(!t) return JSON.stringify({error:'no-save'});
              t.click();
              return 'clicked';
            })()"""
        )
        pg.pump(1)
        state = pg.eval(
            """(()=>{
              const n=app.graph._nodes[0];
              const w=(n.widgets||[]).find(x=>x.name==='state');
              const v=w?w.value:'';
              let parsed=null, keys=[];
              try{parsed=JSON.parse(v);keys=Object.keys(parsed);}catch(e){}
              return JSON.stringify({len:(v||'').length, keys,
                cam: parsed?.camera?.position || null});
            })()"""
        )
        print("[6] 保存到节点:", sv, state)
        try:
            s2 = json.loads(state or "{}")
            ok &= s2.get("len", 0) > 20 and "camera" in (s2.get("keys") or [])
        except Exception:
            ok = False

        bad = [e for e in pg.errors if "director" in e.lower()]
        bad += [c for c in pg.console if "director" in c.lower()]
        print(f"[7] 导演台相关异常: {len(bad)}")
        for b in bad[:8]:
            print("    !", b)
        ok &= not bad

    finally:
        try:
            pg.ws.close()
        except Exception:
            pass
        proc.terminate()

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
