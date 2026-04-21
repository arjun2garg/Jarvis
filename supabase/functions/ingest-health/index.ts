// Jarvis — Apple Health webhook receiver
// Receives Health Auto Export POSTs, normalizes, and upserts into
// raw_apple_health. Staging views do the rest.
//
// Deploy:   supabase functions deploy ingest-health
// Secret:   supabase secrets set HEALTH_WEBHOOK_SECRET=<random>

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Metrics that carry a simple qty field (everything except sleep).
const SCALAR_METRICS = new Set([
  "weight_body_mass",
  "body_fat_percentage",
  "lean_body_mass",
  "body_mass_index",
  "resting_heart_rate",
  "heart_rate_variability_sdnn",
  "cardio_recovery",
  "walking_heart_rate_average",
  "respiratory_rate",
  "dietary_energy_consumed",
  "protein",
  "carbohydrates",
  "total_fat",
  "fiber",
  "sodium",
  "active_energy_burned",
  "basal_energy_burned",
  "step_count",
  "apple_exercise_time",
]);

Deno.serve(async (req: Request) => {
  const secret = req.headers.get("x-webhook-secret");
  if (secret !== Deno.env.get("HEALTH_WEBHOOK_SECRET")) {
    return new Response("Unauthorized", { status: 401 });
  }

  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  let body: any;
  try {
    body = await req.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const metrics: any[] = body?.data?.metrics ?? [];
  if (metrics.length === 0) {
    return new Response(JSON.stringify({ upserted: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // One row per (metric, day). Dedup within this payload so upsert
  // doesn't see a repeated key in the same batch.
  const byKey = new Map<string, Record<string, any>>();

  for (const metric of metrics) {
    const metricName: string = metric.name;
    const unit: string | null = metric.units ?? null;
    const dataPoints: any[] = metric.data ?? [];

    for (const point of dataPoints) {
      const rawDate: string = point.date ?? "";
      const recordedDate = rawDate.substring(0, 10);
      if (!recordedDate || recordedDate.length < 10) continue;

      const isScalar = SCALAR_METRICS.has(metricName);
      const value = isScalar ? (point.qty ?? null) : null;

      byKey.set(`${metricName}|${recordedDate}`, {
        metric_name: metricName,
        recorded_date: recordedDate,
        value,
        unit,
        data_point: point,
        synced_at: new Date().toISOString(),
      });
    }
  }

  const rows = Array.from(byKey.values());
  if (rows.length === 0) {
    return new Response(JSON.stringify({ upserted: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const CHUNK = 500;
  let totalUpserted = 0;

  for (let i = 0; i < rows.length; i += CHUNK) {
    const chunk = rows.slice(i, i + CHUNK);
    const { error } = await supabase
      .from("raw_apple_health")
      .upsert(chunk, { onConflict: "metric_name,recorded_date" });

    if (error) {
      console.error("Upsert error:", error.message);
      await supabase.from("sync_log").insert({
        source: "apple_health",
        records_synced: totalUpserted,
        status: "error",
        error_message: error.message,
      });
      return new Response(JSON.stringify({ error: error.message }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    totalUpserted += chunk.length;
  }

  await supabase.from("sync_log").insert({
    source: "apple_health",
    records_synced: totalUpserted,
    status: "success",
  });

  console.log(`Upserted ${totalUpserted} health data points`);
  return new Response(JSON.stringify({ upserted: totalUpserted }), {
    headers: { "Content-Type": "application/json" },
  });
});
