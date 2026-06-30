// Verifies the JS -> Python HTTP host bridge end to end.
source.enable = function () { log("nettest.enable"); };

source.getHome = function () {
  var resp = http.GET("https://example.com", {});
  log("nettest GET code=" + resp.code + " len=" + (resp.body ? resp.body.length : 0));
  var item = new PlatformVideo({
    id: new PlatformID("NetTest", "1", "nettest"),
    name: "HTTP code " + resp.code + " (" + (resp.body ? resp.body.length : 0) + " bytes)",
    url: "nettest://1",
    thumbnails: new Thumbnails([]),
  });
  return new VideoPager([item], false, {});
};
