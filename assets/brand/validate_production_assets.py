"""Focused checks for production Live Stack asset derivation and consumers."""
from __future__ import annotations
import argparse,hashlib,json,os,re,shutil,subprocess,tempfile,xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parent; REPO=ROOT.parents[1]; STATIC=REPO/"token_tracker"/"static"; NS="{http://www.w3.org/2000/svg}"
EXPECTED="96e1c4f66cd9e094b181889ea92ebd983f56bfc6fad172e51a3bb9d162cefae6"
SIZES=(16,20,24,32,40,48,64,128,256)
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--node",default=os.environ.get("BRAND_NODE") or shutil.which("node")); ap.add_argument("--node-modules",default=os.environ.get("BRAND_NODE_MODULES") or os.environ.get("NODE_PATH")); a=ap.parse_args()
 if not a.node or not a.node_modules: raise SystemExit("Pass --node and --node-modules for existing sharp")
 master=ROOT/"mark-master.svg"; assert sha(master)==EXPECTED
 src=ET.parse(master).getroot(); sg=src.find(NS+"g"); source_paths=sg.findall(NS+"path"); ds=[x.get("d") for x in source_paths]; transform=sg.get("transform"); vb=src.get("viewBox")
 assert len(ds)==4 and sum(d.count("C") for d in ds)==18 and sum(sum(d.count(c) for c in "LHV") for d in ds)==17 and sum(d.count("Z") for d in ds)==4
 variants={"mark-full.svg":["#F2F0EA","#B9ADF5","#F2F0EA","#F2F0EA"],"mark-mono-dark.svg":["#F2F0EA"]*4}
 for name,fills in variants.items():
  r=ET.parse(ROOT/name).getroot(); g=r.find(NS+"g"); ps=g.findall(NS+"path"); assert g.get("transform")==transform and r.get("viewBox")==vb and [p.get("d") for p in ps]==ds and [p.get("fill") for p in ps]==fills
 # Light mono and production tile keep the same source paths/group under one nested viewport.
 for name in ("mark-mono-light.svg","icon-tile.svg"):
  r=ET.parse(ROOT/name).getroot(); g=r.find(".//"+NS+"g"); assert g.get("transform")==transform and [p.get("d") for p in g.findall(NS+"path")]==ds
 light=ET.parse(ROOT/"mark-mono-light-transparent.svg").getroot(); lg=light.find(NS+"g"); assert lg.get("transform")==transform and [p.get("d") for p in lg.findall(NS+"path")]==ds and [p.get("fill") for p in lg.findall(NS+"path")]==["#0B0B0D"]*4
 word=ET.parse(ROOT/"wordmark.svg").getroot().find(NS+"path")
 files=[(ROOT/"mark-full.svg",ROOT/"mark-full-512.png",(512,round(512*float(src.get("height"))/float(src.get("width"))))),
        (ROOT/"mark-mono-dark.svg",ROOT/"mark-mono-dark-512.png",(512,round(512*float(src.get("height"))/float(src.get("width")))))]
 for name in ("lockup-dark.svg","lockup-light.svg","lockup.svg"):
  r=ET.parse(ROOT/name).getroot(); nested=r.find(NS+"svg"); g=nested.find(NS+"g")
  assert g.get("transform")==transform and [p.get("d") for p in g.findall(NS+"path")]==ds
  w=r.find(NS+"path"); assert w.get("d")==word.get("d") and w.get("fill-rule")=="evenodd"
 assert (ROOT/"lockup.svg").read_bytes()==(ROOT/"lockup-dark.svg").read_bytes()
 files.extend([(ROOT/"lockup-dark.svg",ROOT/"lockup-dark.png",(1280,640)),(ROOT/"lockup-light.svg",ROOT/"lockup-light.png",(1280,640)),(ROOT/"lockup-dark.svg",ROOT/"social-preview.png",(1280,640))])
 for path in (STATIC/"phone-icon-192.png",STATIC/"phone-icon-512.png",STATIC/"apple-touch-icon.png"):
  n=180 if path.name.startswith("apple") else int(path.stem.rsplit("-",1)[1]); files.append((STATIC/"phone-icon.svg",path,(n,n)))
 for mode in ("full","mono-dark","mono-light"):
  for n in (256,512,1024):
   p=ROOT/f"mark-{mode}-{n}.png"; h=round(n*float(src.get("height"))/float(src.get("width")))
   with Image.open(p) as im: assert im.size==(n,h) and im.mode=="RGBA"
   source_svg=ROOT/f"mark-{mode}.svg" if mode!="mono-light" else ROOT/"mark-mono-light-transparent.svg"
   files.append((source_svg,p,(n,h)))
 for p in (ROOT/"branchglance.ico",STATIC/"favicon.ico"):
  with Image.open(p) as im: assert {w for w,h in im.ico.sizes()}==set(SIZES)
 for s in (STATIC/"phone-icon.svg",STATIC/"favicon.svg"):
  r=ET.parse(s).getroot(); g=r.find(".//"+NS+"g"); assert g.get("transform")==transform and [p.get("d") for p in g.findall(NS+"path")] == ds
 jobs=[]
 with tempfile.TemporaryDirectory(prefix="brand-prod-check-") as td:
  td=Path(td)
  for i,(svg,out,dim) in enumerate(files): jobs.append({"source":str(svg),"output":str(td/f"{i}.png"),"width":dim[0],"height":dim[1]})
  ico_checks=[]
  for ico_path in (ROOT/"branchglance.ico",STATIC/"favicon.ico"):
   for n in SIZES:
    frame_path=td/f"ico-{ico_path.stem}-{n}.png"
    jobs.append({"source":str(STATIC/"phone-icon.svg"),"output":str(frame_path),"width":n,"height":n})
    ico_checks.append((ico_path,n,frame_path))
  env=os.environ.copy(); env["NODE_PATH"]=a.node_modules; subprocess.run([a.node,str(ROOT/"render_svgs.js")],input=json.dumps(jobs),text=True,check=True,env=env)
  for j,(_,out,_) in zip(jobs,files):
   with Image.open(j["output"]) as x,Image.open(out) as y: assert x.size==y.size and x.mode==y.mode and x.tobytes()==y.tobytes(),out
  for ico_path,n,frame_path in ico_checks:
   with Image.open(frame_path) as render,Image.open(ico_path) as ico:
    frame=ico.ico.getimage((n,n)).convert("RGBA")
    assert frame.tobytes()==render.convert("RGBA").tobytes(),f"ICO frame is not same-SVG raster: {ico_path} {n}px"
 print(f"PASS master hash/path/color/transform, production SVG linkage, {len(files)} Sharp raster parity checks, PNG dimensions/alpha, nine ICO frames")
if __name__=="__main__": main()
