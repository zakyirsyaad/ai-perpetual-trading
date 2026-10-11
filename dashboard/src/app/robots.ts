import type { MetadataRoute } from "next";

/** A private operator dashboard: never indexed. */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      disallow: "/",
    },
  };
}
