#region Copyright & License Information
/*
 * Copyright (c) The OpenRA Developers and Contributors
 * This file is part of the WarpTest Lazarus test overlay. It is installed only
 * into disposable C1-Lazarus materializations.
 */
#endregion

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using OpenRA.Graphics;
using OpenRA.Mods.Common.Widgets;
using OpenRA.Primitives;
using OpenRA.Traits;
using OpenRA.Widgets;

namespace OpenRA.Mods.Common.Traits
{
	[TraitLocation(SystemActors.World)]
	[Desc("Internal WarpTest Lazarus overlay: reproduce ProductionTabsWidget cache synchronization.")]
	public sealed class WarptestProductionTabsAuditInfo : TraitInfo<WarptestProductionTabsAudit> { }

	public sealed class WarptestProductionTabsAudit : IWorldLoaded, ITick
	{
		World world;
		Player localPlayer;
		Player nonLocalPlayer;
		bool disabledProducerCreated;
		bool nonLocalProducerCreated;
		bool gateCreated;
		bool written;
		readonly List<object> snapshots = [];

		public void WorldLoaded(World w, WorldRenderer wr)
		{
			world = w;
			localPlayer = world.LocalPlayer;
			nonLocalPlayer = world.Players.FirstOrDefault(p => p != localPlayer && !p.NonCombatant);
		}

		void ITick.Tick(Actor self)
		{
			if (world == null || written)
				return;

			localPlayer ??= world.LocalPlayer;
			if (localPlayer == null)
				return;
			nonLocalPlayer ??= world.Players.FirstOrDefault(p => p != localPlayer && !p.NonCombatant);
			if (nonLocalPlayer == null)
				return;

			if (!disabledProducerCreated && world.WorldTick >= 8)
			{
				CreateActor("WARPTABDISABLED", localPlayer, new CPos(41, 48));
				disabledProducerCreated = true;
			}
			else if (!nonLocalProducerCreated && world.WorldTick >= 24)
			{
				// PR #21906 specifically fixes this cross-player sequence: an enemy
				// production actor is added while the local queue remains disabled.
				CreateActor("WARPTABENABLED", nonLocalPlayer, new CPos(46, 48));
				nonLocalProducerCreated = true;
			}
			else if (!gateCreated && world.WorldTick >= 48)
			{
				CreateActor("WARPTABGATE", localPlayer, new CPos(51, 48));
				gateCreated = true;
			}

			if (world.WorldTick == 40 || (gateCreated && (world.WorldTick == 60 || world.WorldTick == 100 || world.WorldTick == 160 || world.WorldTick == 240)))
				CaptureSnapshot();

			if (gateCreated && world.WorldTick >= 240)
			{
				WriteAudit();
				written = true;
			}
		}

		Actor CreateActor(string type, Player owner, CPos cell)
		{
			var actor = world.CreateActor(false, type, new TypeDictionary
			{
				new OwnerInit(owner),
				new FactionInit(owner.Faction.InternalName),
				new LocationInit(cell),
			});
			world.AddFrameEndTask(w => w.Add(actor));
			return actor;
		}

		void CaptureSnapshot()
		{
			var queues = world.ActorsWithTrait<ProductionQueue>()
				.Where(p => p.Actor.Owner == localPlayer && p.Actor.IsInWorld)
				.Select(p => new { p.Actor.Info.Name, p.Trait.Info.Group, p.Trait.Enabled, Owner = p.Actor.Owner.InternalName })
				.OrderBy(p => p.Name)
				.ToArray();
			var tabs = Ui.Root.GetOrNull<ProductionTabsWidget>("PRODUCTION_TABS");
			if (tabs != null && tabs.Groups.ContainsKey("Vehicle"))
				tabs.QueueGroup = "Vehicle";
			var vehicleTabs = tabs != null && tabs.Groups.TryGetValue("Vehicle", out var group)
				? group.Tabs.Count
				: -1;
			var warptabTabs = tabs != null && tabs.Groups.TryGetValue("Vehicle", out group)
				? group.Tabs.Count(t => t.Queue.Actor.Info.Name.StartsWith("warptab", StringComparison.Ordinal))
				: -1;
			snapshots.Add(new { worldTick = world.WorldTick, vehicleTabs, warptabTabs, queues });
		}

		void WriteAudit()
		{
			var destination = Environment.GetEnvironmentVariable("WARPTEST_OPENRA_TABS_AUDIT_PATH");
			if (string.IsNullOrEmpty(destination))
				return;

			var payload = new
			{
				localPlayer = localPlayer.InternalName,
				nonLocalProductionActor = nonLocalPlayer?.InternalName,
				snapshots,
			};
			Directory.CreateDirectory(Path.GetDirectoryName(destination));
			File.WriteAllText(destination, JsonSerializer.Serialize(payload));
		}
	}
}
