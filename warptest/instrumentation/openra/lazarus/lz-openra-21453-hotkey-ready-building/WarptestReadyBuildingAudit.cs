#region Copyright & License Information
/*
 * Internal WarpTest Lazarus fixture.  This source is copied only into a
 * disposable materialized OpenRA clone; it is never installed in the pinned
 * source submodule.
 */
#endregion

using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using OpenRA.Graphics;
using OpenRA.Mods.Common.Orders;
using OpenRA.Mods.Common.Widgets;
using OpenRA.Traits;
using OpenRA.Widgets;

namespace OpenRA.Mods.Common.Traits
{
	[TraitLocation(SystemActors.World)]
	[Desc("Internal Lazarus fixture: prepares and observes a hidden completed building.")]
	public sealed class WarptestReadyBuildingAuditInfo : TraitInfo<WarptestReadyBuildingAudit> { }

	public sealed class WarptestReadyBuildingAudit : IWorldLoaded, ITick
	{
		const string Target = "warpready";
		const string QueueType = "Building";
		const string AuditPathEnvironment = "WARPTEST_OPENRA_HOTKEY_AUDIT_PATH";
		const string StatePathEnvironment = "WARPTEST_OPENRA_HOTKEY_STATE_PATH";

		World world;
		WorldRenderer worldRenderer;
		ProductionQueue queue;
		bool queued;
		bool stateRestoreRequested;
		bool stateRestoreApplied;
		string stateCheckpointSha256 = "";
		string stateRestoreSpecSha256 = "";
		string stateRestoreError = "";
		bool observedPlacementMode;
		bool previousPlacementActive;
		bool placementCancelObserved;

		void IWorldLoaded.WorldLoaded(World w, WorldRenderer wr)
		{
			world = w;
			worldRenderer = wr;
		}

		void ITick.Tick(Actor self)
		{
			if (world == null || world.LocalPlayer == null)
				return;

			if (!queued)
				TryQueueTarget();

			TryRestoreState();
			ObservePlacementTransitions();

			WriteAudit();
		}

		void TryQueueTarget()
		{
			queue = world.LocalPlayer.PlayerActor.TraitsImplementing<ProductionQueue>()
				.FirstOrDefault(q => q.Info.Type == QueueType);
			if (queue == null || !queue.Enabled || !queue.BuildableItems().Any(a => a.Name == Target))
				return;

			queue.Actor.ResolveOrder(Order.StartProduction(queue.Actor, Target, 1, false));
			queued = queue.AllQueued().Any(item => item.Item == Target);
		}

		void ObservePlacementTransitions()
		{
			var placementActive = world.OrderGenerator is PlaceBuildingOrderGenerator;
			if (!observedPlacementMode)
			{
				observedPlacementMode = true;
				previousPlacementActive = placementActive;
				return;
			}

			if (previousPlacementActive && !placementActive)
				placementCancelObserved = true;
			previousPlacementActive = placementActive;
		}

		void TryRestoreState()
		{
			if (stateRestoreApplied || !string.IsNullOrEmpty(stateRestoreError) || queue == null || worldRenderer == null)
				return;

			var path = Environment.GetEnvironmentVariable(StatePathEnvironment);
			if (string.IsNullOrEmpty(path))
				return;

			stateRestoreRequested = true;
			var queueItem = queue.CurrentItem();
			if (queueItem == null || !queueItem.Done)
				return;

			try
			{
				using (var document = JsonDocument.Parse(File.ReadAllText(path)))
				{
					var root = document.RootElement;
					if (!root.TryGetProperty("schema_version", out var schema) || schema.GetInt32() != 1)
						throw new InvalidOperationException("state restore has an unknown schema");
					if (!root.TryGetProperty("checkpoint_sha256", out var checkpoint))
						throw new InvalidOperationException("state restore has no checkpoint SHA");
					if (!root.TryGetProperty("semantic_start", out var semanticStart) || semanticStart.ValueKind != JsonValueKind.Object)
						throw new InvalidOperationException("state restore has no generated semantic start");
					if (!root.TryGetProperty("semantic_start_sha256", out var semanticSha))
						throw new InvalidOperationException("state restore has no semantic start SHA");

					stateCheckpointSha256 = checkpoint.GetString() ?? "";
					if (stateCheckpointSha256.Length != 64 || !stateCheckpointSha256.All(c => "0123456789abcdef".Contains(c)))
						throw new InvalidOperationException("state restore has an invalid checkpoint SHA");
					stateRestoreSpecSha256 = semanticSha.GetString() ?? "";
					if (stateRestoreSpecSha256.Length != 64 || !stateRestoreSpecSha256.All(c => "0123456789abcdef".Contains(c)))
						throw new InvalidOperationException("state restore has an invalid semantic start SHA");
					var requestedTarget = ResolveSemanticPlacementTarget(semanticStart, queue);
					Sync.RunUnsynced(world, () =>
						world.OrderGenerator = new PlaceBuildingOrderGenerator(queue, requestedTarget, worldRenderer));
				}

				stateRestoreApplied = world.OrderGenerator is PlaceBuildingOrderGenerator;
				if (!stateRestoreApplied)
					stateRestoreError = "state restore did not activate building placement";
			}
			catch (Exception e)
			{
				stateRestoreError = e.Message;
				Log.Write("debug", $"Unable to restore Lazarus OpenRA hotkey state: {e.Message}");
			}
		}

		string ResolveSemanticPlacementTarget(JsonElement semanticStart, ProductionQueue requestedQueue)
		{
			if (!semanticStart.TryGetProperty("schema", out var schema)
				|| schema.GetString() != "c1-lazarus-openra-generated-start-v1")
				throw new InvalidOperationException("state restore has an unexpected semantic schema");
			if (!semanticStart.TryGetProperty("target", out var target) || target.ValueKind != JsonValueKind.Object
				|| !target.TryGetProperty("kind", out var kind) || kind.GetString() != "openra_map"
				|| !target.TryGetProperty("mod", out var mod) || mod.GetString() != "ra"
				|| !target.TryGetProperty("map", out var map) || map.GetString() != "allies-08a")
				throw new InvalidOperationException("state restore has an unexpected generated map target");
			if (!semanticStart.TryGetProperty("action", out var action) || action.ValueKind != JsonValueKind.Object
				|| !action.TryGetProperty("type", out var type) || type.GetString() != "openra_build_actor"
				|| !action.TryGetProperty("queue", out var requestedQueueType) || requestedQueueType.GetString() != QueueType
				|| !action.TryGetProperty("selector", out var selector)
				|| selector.GetString() != "completed_building_placement")
				throw new InvalidOperationException("state restore has an unexpected generated production action");
			if (requestedQueue.Info.Type != requestedQueueType.GetString())
				throw new InvalidOperationException("state restore action does not select the active production queue");
			return Target;
		}

		void WriteAudit()
		{
			var path = Environment.GetEnvironmentVariable(AuditPathEnvironment);
			if (string.IsNullOrEmpty(path) || queue == null)
				return;

			var palette = Ui.Root.GetOrNull<ProductionPaletteWidget>("PRODUCTION_PALETTE");
			if (palette != null && palette.CurrentQueue != queue)
				palette.CurrentQueue = queue;

			var items = queue.AllItems()
				.OrderBy(a => a.TraitInfo<BuildableInfo>().BuildPaletteOrder)
				.ToArray();
			var targetIndex = Array.FindIndex(items, item => item.Name == Target);
			var visibleStart = palette != null ? palette.IconRowOffset * palette.Columns : 0;
			var visibleEnd = visibleStart + (palette?.DisplayedIconCount ?? 0);
			var targetHidden = targetIndex >= 0 && (targetIndex < visibleStart || targetIndex >= visibleEnd);
			var queueItem = queue.CurrentItem();
			var payload = new
			{
				schema_version = 1,
				world_tick = world.WorldTick,
				player = world.LocalPlayer.InternalName,
				queue_type = QueueType,
				target = Target,
				queue_item = queueItem?.Item ?? "",
				queue_ready = queueItem?.Done ?? false,
				target_hidden = targetHidden,
				palette_items = items.Length,
				placement_active = world.OrderGenerator is PlaceBuildingOrderGenerator,
				placement_cancel_observed = placementCancelObserved,
				state_restore_requested = stateRestoreRequested,
				state_restore_applied = stateRestoreApplied,
				state_checkpoint_sha256 = stateCheckpointSha256,
				state_restore_spec_sha256 = stateRestoreSpecSha256,
				state_restore_error = stateRestoreError,
				probe_errors = Array.Empty<string>(),
			};

			try
			{
				var absolute = Path.GetFullPath(path);
				var parent = Path.GetDirectoryName(absolute);
				if (string.IsNullOrEmpty(parent))
					return;
				Directory.CreateDirectory(parent);
				var temporary = absolute + ".tmp";
				File.WriteAllText(temporary, JsonSerializer.Serialize(payload));
				File.Move(temporary, absolute, true);
			}
			catch (Exception e)
			{
				Log.Write("debug", $"Unable to write Lazarus OpenRA hotkey audit: {e.Message}");
			}
		}
	}
}
