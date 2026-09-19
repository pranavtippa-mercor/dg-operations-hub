import type { NextConfig } from "next";
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  basePath: process.env.GITHUB_PAGES === "true" ? "/dg-operations-hub" : "",
  images: { unoptimized: true },
};
export default nextConfig;
