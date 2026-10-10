import pathlib

HERE = pathlib.Path(__file__).parent
template = (HERE / "slide_template.html").read_text()

def content_slide(kicker, headline, sub, image):
    return f"""
  <main>
    <div class="text-col">
      <div class="kicker">{kicker}</div>
      <div class="headline tight">{headline}</div>
      <div class="sub">{sub}</div>
    </div>
    <div class="visual-col">
      <div class="device-frame"><img src="../frames/{image}"></div>
    </div>
  </main>
"""

def brand_slide(headline, tagline):
    return f"""
  <div class="brand-frame">
    <div class="headline">{headline}</div>
    <div class="tagline">{tagline}</div>
  </div>
"""

scenes = {
    "scene1": content_slide(
        "Catarroja, Valencia",
        "Thirty Thousand People.<br>One Midday Surplus.",
        "A sun-drenched town near Valencia, mapped building by building.",
        "map_overview_native_crop.png",
    ),
    "scene2": content_slide(
        "Direct action",
        "Public Buildings",
        "Schools, the city hall, clinics — identified for direct municipal investment.",
        "map_public_native_crop.png",
    ),
    "scene3": content_slide(
        "Subsidies",
        "Private Rooftops",
        "Homes, shops and factories — eligible for a solar subsidy.",
        "map_private_native_crop.png",
    ),
    "scene4": content_slide(
        "Zone by zone",
        "Every Hour,<br>Every Season",
        "We segment the grid and simulate what solar and batteries change.",
        "zone_screen.png",
    ),
    "scene5": content_slide(
        "Capital allocation",
        "The Planner",
        "Any budget, split between direct capex and private subsidies — always where it works hardest.",
        "planner_screen.png",
    ),
    "scene6": brand_slide("EveryRay", "Leave No Ray Behind."),
}

for name, content in scenes.items():
    html = template.replace("  <!-- CONTENT_SLOT -->\n", content)
    (HERE / "slides" / f"{name}.html").write_text(html)
    print(f"wrote {name}.html")
