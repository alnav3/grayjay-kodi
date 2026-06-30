// Exercises the domParser package against the host (bs4-backed) DOM.
source.enable = function () { log("domtest.enable"); };

var HTML =
  "<html><body>" +
  "<div id='main' class='wrap big'>" +
  "  <a class='vid' href='/watch?v=1' data-id='1'>First Video</a>" +
  "  <a class='vid' href='/watch?v=2' data-id='2'>Second Video</a>" +
  "  <span class='note'>ignore me</span>" +
  "</div></body></html>";

source.getHome = function () {
  var doc = domParser.parseFromString(HTML);

  var main = doc.getElementById("main");
  log("main.className = " + main.className);
  log("main.tagName = " + main.tagName);

  var links = doc.querySelectorAll("a.vid");
  log("querySelectorAll a.vid -> " + links.length + " nodes");

  var items = [];
  for (var i = 0; i < links.length; i++) {
    var a = links[i];
    items.push(new PlatformVideo({
      id: new PlatformID("DomTest", a.getAttribute("data-id"), "domtest"),
      name: a.textContent + " [" + a.getAttribute("href") + "]",
      url: "domtest://" + a.getAttribute("data-id"),
      thumbnails: new Thumbnails([]),
    }));
  }

  var first = doc.querySelector("a.vid");
  log("querySelector first text = " + first.text);
  log("byTag a count = " + doc.getElementsByTagName("a").length);
  log("byClass note count = " + doc.getElementsByClassName("note").length);

  return new VideoPager(items, false, {});
};
