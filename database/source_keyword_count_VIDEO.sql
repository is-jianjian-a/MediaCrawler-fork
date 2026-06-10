SELECT 
    source_keyword,
    COUNT(*) AS note_count,
    SUM(COUNT(*)) OVER() AS total_count
FROM xhs_note
WHERE source_keyword LIKE '%视频%'
GROUP BY source_keyword
ORDER BY note_count DESC;