"""Speaker-segment matching and rendering."""

import re
import streamlit as st


def find_matching_segments_with_context(speaker_segments, search_term, context_size=10):
    """Find speaker segments that match search term and return with context"""
    if not speaker_segments or not search_term:
        return []
    
    # Sort segments by start time
    sorted_segments = sorted(speaker_segments, key=lambda x: x.get('start_time', 0))
    
    # Find segments containing the search term
    matching_indices = []
    for i, segment in enumerate(sorted_segments):
        text = segment.get('text', '').lower()
        if search_term.lower() in text:
            matching_indices.append(i)
    
    if not matching_indices:
        return []
    
    # Extract context around matches
    context_segments = []
    added_indices = set()
    
    for match_idx in matching_indices:
        # Calculate context range
        start_idx = max(0, match_idx - context_size)
        end_idx = min(len(sorted_segments), match_idx + context_size + 1)
        
        # Add segments in context range
        for i in range(start_idx, end_idx):
            if i not in added_indices:
                segment = sorted_segments[i].copy()
                segment['is_match'] = (i == match_idx)
                segment['context_group'] = match_idx  # Group segments by their match
                context_segments.append(segment)
                added_indices.add(i)
    
    # Sort by start time to maintain chronological order
    return sorted(context_segments, key=lambda x: x.get('start_time', 0))


def highlight_text(text, search_term):
    """Highlight search term in text"""
    if not search_term or not text:
        return text
    
    # Simple highlighting by making search term bold
    pattern = re.compile(re.escape(search_term), re.IGNORECASE)
    return pattern.sub(f"**{search_term.upper()}**", str(text))


def display_search_result_with_speakers(speaker_segments, search_term, file_info=None):
    """Display search results with speaker segments and context"""
    if not speaker_segments:
        st.info("No speaker segments available for this search result.")
        return
    
    # Display file info if available
    if file_info:
        st.markdown(f"""
        **File:** {file_info.get('filename', 'Unknown')} | 
        **Language:** {file_info.get('language', 'Unknown')} | 
        **Duration:** {file_info.get('duration', 0):.1f}s |
        **Speakers:** {file_info.get('speaker_count', 'N/A')}
        """)
    
    # Group consecutive segments by speaker to reduce repetition (similar to display_speaker_transcript)
    grouped_segments = []
    current_speaker = None
    current_text = ""
    current_start = None
    current_end = None
    current_is_match = False
    current_context_group = None
    
    for segment in speaker_segments:
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        is_match = segment.get('is_match', False)
        context_group = segment.get('context_group')
        
        # Only group if same speaker, close timing, same match status, and same context group
        if (speaker == current_speaker and 
            current_end and abs(start_time - current_end) < 2 and
            is_match == current_is_match and
            context_group == current_context_group):
            # Same speaker, close timing, same match status - combine segments
            current_text += " " + text
            current_end = end_time
        else:
            # Different speaker, gap in time, or different match status - save previous and start new
            if current_speaker is not None:
                grouped_segments.append({
                    'speaker': current_speaker,
                    'text': current_text,
                    'start_time': current_start,
                    'end_time': current_end,
                    'is_match': current_is_match,
                    'context_group': current_context_group
                })
            
            current_speaker = speaker
            current_text = text
            current_start = start_time
            current_end = end_time
            current_is_match = is_match
            current_context_group = context_group
    
    # Don't forget the last segment
    if current_speaker is not None:
        grouped_segments.append({
            'speaker': current_speaker,
            'text': current_text,
            'start_time': current_start,
            'end_time': current_end,
            'is_match': current_is_match,
            'context_group': current_context_group
        })
    
    # Display the grouped segments with context
    current_context_group = None
    match_count = 0
    
    for i, segment in enumerate(grouped_segments):
        speaker = segment['speaker']
        text = segment['text']
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        is_match = segment.get('is_match', False)
        context_group = segment.get('context_group')
        
        # Add separator between different context groups
        if context_group != current_context_group and current_context_group is not None:
            st.markdown("---")
        
        if context_group != current_context_group:
            current_context_group = context_group
            if is_match:
                match_count += 1
                st.markdown(f"**🎯 Match {match_count}:**")
        
        # Format time as MM:SS
        start_mins, start_secs = divmod(int(start_time), 60)
        end_mins, end_secs = divmod(int(end_time), 60)
        time_range = f"{start_mins:02d}:{start_secs:02d} - {end_mins:02d}:{end_secs:02d}"
        
        # Highlight search term in matching segments
        display_text = text
        if is_match:
            display_text = highlight_text(text, search_term)
        
        # Different styling for match vs context
        if is_match:
            # Highlight the matching segment
            st.markdown(f"""
            <div class="speaker-segment" style="border-left-color: #FF5722; background-color: #FFF3E0;">
                <div class="speaker-label" style="color: #E65100;">{speaker} <span class="timestamp">({time_range}) 🎯 MATCH</span></div>
                <div class="speaker-text" style="font-weight: 500;">{display_text}</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            # Regular context segment
            st.markdown(f"""
            <div class="speaker-segment" style="border-left-color: #9E9E9E; background-color: #FAFAFA;">
                <div class="speaker-label" style="color: #616161;">{speaker} <span class="timestamp">({time_range})</span></div>
                <div class="speaker-text" style="color: #757575;">{display_text}</div>
            </div>
            """, unsafe_allow_html=True)


def display_speaker_transcript(speaker_segments, file_info=None):
    """Display transcript with speaker segments line by line"""
    if not speaker_segments:
        st.info("No speaker segments available for this file.")
        return
    
    # Display file info if available
    if file_info:
        st.markdown(f"""
        **File:** {file_info.get('filename', 'Unknown')} | 
        **Language:** {file_info.get('language', 'Unknown')} | 
        **Duration:** {file_info.get('duration', 0):.1f}s
        """)
        st.divider()
    
    # Sort segments by start time
    sorted_segments = sorted(speaker_segments, key=lambda x: x.get('start_time', 0))
    
    # Group consecutive segments by speaker to reduce repetition
    grouped_segments = []
    current_speaker = None
    current_text = ""
    current_start = None
    current_end = None
    
    for segment in sorted_segments:
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        
        if speaker == current_speaker and current_end and abs(start_time - current_end) < 2:
            # Same speaker, close timing - combine segments
            current_text += " " + text
            current_end = end_time
        else:
            # Different speaker or gap in time - save previous and start new
            if current_speaker is not None:
                grouped_segments.append({
                    'speaker': current_speaker,
                    'text': current_text,
                    'start_time': current_start,
                    'end_time': current_end
                })
            
            current_speaker = speaker
            current_text = text
            current_start = start_time
            current_end = end_time
    
    # Don't forget the last segment
    if current_speaker is not None:
        grouped_segments.append({
            'speaker': current_speaker,
            'text': current_text,
            'start_time': current_start,
            'end_time': current_end
        })
    
    # Display the grouped segments
    for i, segment in enumerate(grouped_segments):
        speaker = segment['speaker']
        text = segment['text']
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        
        # Format time as MM:SS
        start_mins, start_secs = divmod(int(start_time), 60)
        end_mins, end_secs = divmod(int(end_time), 60)
        time_range = f"{start_mins:02d}:{start_secs:02d} - {end_mins:02d}:{end_secs:02d}"
        
        # Display the segment
        st.markdown(f"""
        <div class="speaker-segment">
            <div class="speaker-label">{speaker} <span class="timestamp">({time_range})</span></div>
            <div class="speaker-text">{text}</div>
        </div>
        """, unsafe_allow_html=True)
