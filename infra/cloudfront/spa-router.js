// CloudFront Function: SPA routing for app-dev/app.yoloscribe.com
// Attached to the SPA distribution default behavior (viewer-request).
// Rewrites all paths to /index.html except /assets/* and */media/* paths,
// which are passed through so the SPA OAC 403s them (media) or serves them
// directly (assets) without masking auth errors with a fake 200.
function handler(event) {
    var request = event.request;
    var uri = request.uri;

    if (uri.startsWith('/assets/') || uri.indexOf('/media/') !== -1) {
        return request;
    }

    request.uri = '/index.html';
    return request;
}
