/** API calls go through this origin so the session cookie stays first-party. */
const API_URL = process.env.API_URL || "http://localhost:8000";
export default {
  output: "standalone",
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};
