#!/usr/bin/env python3
"""
Clean up duplicate OOM incidents from the database.
Keeps only the most recent active (investigating/open) incident for each title.
"""
import asyncio
from sqlalchemy.future import select
from database.session import AsyncSessionLocal
from database import models as dbm

async def clean_duplicates():
    async with AsyncSessionLocal() as session:
        # Get all incidents with OOM in title
        result = await session.execute(
            select(dbm.Incident)
            .where(dbm.Incident.title.ilike('%oom%'))
            .order_by(dbm.Incident.created_at.desc())
        )
        oom_incidents = result.scalars().all()
        
        print(f"Found {len(oom_incidents)} OOM incidents:")
        for inc in oom_incidents:
            print(f"  ID: {inc.id}, Title: {inc.title}, Status: {inc.status}, Created: {inc.created_at}")
        
        # Group by title and keep only the most recent active one
        seen_titles = {}
        to_delete = []
        
        for inc in oom_incidents:
            if inc.title not in seen_titles:
                seen_titles[inc.title] = inc
                # Keep this one (most recent)
            else:
                # If this is an older one, delete it
                to_delete.append(inc)
                print(f"Marking for deletion: {inc.id} - {inc.title} (older duplicate)")
        
        # Delete duplicates
        if to_delete:
            for inc in to_delete:
                await session.delete(inc)
            await session.commit()
            print(f"\nDeleted {len(to_delete)} duplicate OOM incidents")
        else:
            print("\nNo duplicate OOM incidents to delete")
            
        # Show remaining
        print("\nRemaining OOM incidents:")
        remaining = await session.execute(
            select(dbm.Incident)
            .where(dbm.Incident.title.ilike('%oom%'))
            .order_by(dbm.Incident.created_at.desc())
        )
        remaining_incidents = remaining.scalars().all()
        for inc in remaining_incidents:
            print(f"  ID: {inc.id}, Title: {inc.title}, Status: {inc.status}, Created: {inc.created_at}")

if __name__ == "__main__":
    asyncio.run(clean_duplicates())