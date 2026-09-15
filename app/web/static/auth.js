(() => {
  function syncUserProfile(response) {
    const raw = response.headers.get("X-User-Profile");
    const identity = document.querySelector(".nav-identity");
    if (!raw || !identity) return;
    const profile = JSON.parse(raw);
    if (profile.subject !== identity.dataset.subject ||
        BigInt(profile.version) < BigInt(identity.dataset.profileVersion || "0")) return;
    identity.textContent = profile.label;
    identity.title = profile.label;
    identity.dataset.profileVersion = profile.version;
    const avatar = document.querySelector(".nav-avatar");
    if (!avatar) return;
    if (profile.avatar) {
      if (avatar.getAttribute("src") !== profile.avatar) {
        avatar.hidden = false;
        avatar.src = profile.avatar;
      }
    } else {
      avatar.hidden = true;
      avatar.removeAttribute("src");
    }
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, window.location.href);
    if (url.origin !== window.location.origin) return originalFetch(input, options);
    const headers = new Headers(options.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set("X-Requested-With", "fetch");
    headers.set("X-Return-To", window.location.pathname + window.location.search);
    const response = await originalFetch(input, { ...options, headers });
    const loginUrl = response.headers.get("X-Login-URL");
    if (response.status === 401 && loginUrl) {
      window.location.assign(loginUrl);
      throw new Error("登录已过期，正在前往登录页面。");
    }
    if (response.ok) syncUserProfile(response);
    return response;
  };
})();
