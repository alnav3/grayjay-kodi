// Minimal offline test plugin. Exercises the host without any network so the
// bridge / packages.js scaffolding can be validated independently of HTTP and
// of which JS engine backend is active.

var PLATFORM = "Example";

source.enable = function (conf, settings, savedState) {
  log("example.enable called");
};

source.getHome = function (continuationToken) {
  var items = [
    new PlatformVideo({
      id: new PlatformID(PLATFORM, "1", "example"),
      name: "Big Buck Bunny",
      url: "example://video/1",
      thumbnails: new Thumbnails([new Thumbnail("https://test/thumb1.jpg", 720)]),
      author: new PlatformAuthorLink("c1", "Blender", "example://chan/blender"),
      duration: 596,
      viewCount: 12345,
    }),
    new PlatformVideo({
      id: new PlatformID(PLATFORM, "2", "example"),
      name: "Sintel",
      url: "example://video/2",
      thumbnails: new Thumbnails([new Thumbnail("https://test/thumb2.jpg", 1080)]),
      duration: 888,
      viewCount: 6789,
    }),
  ];
  return new VideoPager(items, false, {});
};

source.isContentDetailsUrl = function (url) {
  return url.indexOf("example://video/") === 0;
};

source.getContentDetails = function (url) {
  return new PlatformVideoDetails({
    id: new PlatformID(PLATFORM, "1", "example"),
    name: "Big Buck Bunny",
    url: url,
    description: "Test details",
    duration: 596,
    video: new VideoSourceDescriptor([
      new VideoUrlSource({
        width: 1280, height: 720, container: "video/mp4",
        name: "720p", url: "https://test.example/bbb_720.mp4",
      }),
    ]),
  });
};
